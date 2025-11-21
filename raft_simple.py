import os
import asyncio
import random
import time
import json
import logging
from typing import Optional, List
from enum import Enum
from dataclasses import dataclass
import grpc

import raft_pb2
import raft_pb2_grpc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class NodeState(Enum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"

@dataclass
class LogEntry:
    term: int
    index: int
    ip: str
    timestamp: float

class RaftNode:
    """Improved Raft node with better vote split avoidance and partition handling"""

    def __init__(self, node_id: str, peers: List[str]):
        self.node_id = node_id
        self.peers = peers  # List of peer addresses (host:port)

        # Persistent state
        self.current_term = 0
        self.voted_for: Optional[str] = None
        self.log: List[LogEntry] = []

        # Volatile state
        self.commit_index = 0
        self.last_applied = 0

        # Leader state
        self.next_index = {}
        self.match_index = {}

        # Node state
        self.state = NodeState.FOLLOWER
        self.current_leader: Optional[str] = None
        self.votes_received = set()

        # Timing - randomized to avoid vote splits
        min_timeout = int(os.getenv("RAFT_TIMEOUT_MIN", "1500"))
        max_timeout = int(os.getenv("RAFT_TIMEOUT_MAX", "3000"))
        self.timeout_range = (min_timeout, max_timeout)
        self.last_heartbeat = time.time()
        self.election_timeout = self._random_timeout()
        
        # Heartbeat interval (should be much less than election timeout)
        self.heartbeat_interval = 0.1  # 100ms
        
        # Pre-vote mechanism to reduce disruptions
        self.enable_prevote = True
        self.in_prevote = False

        # gRPC connections
        self.peer_stubs = {}
        self.peer_channels = {}  # Track channels for cleanup
        self._init_peers()

        self.running = False
        self.lock = asyncio.Lock()

        # Ensure each node has unique randomization
        import hashlib
        # Use node_id for consistent but unique seeding
        seed = int(hashlib.sha256(f"{node_id}{time.time()}".encode()).hexdigest(), 16) % (2**32)
        random.seed(seed)
        
        # Add jitter to initial election timeout to avoid synchronized starts
        initial_jitter = random.uniform(0, 0.5)
        self.last_heartbeat = time.time() + initial_jitter

        logger.info(f"Node {self.node_id} initialized with election timeout range: {self.timeout_range}ms")
        logger.info(f"Node {self.node_id} will connect to peers: {self.peers}")

    def _random_timeout(self) -> float:
        """Generate random election timeout to avoid vote splits"""
        return random.randint(self.timeout_range[0], self.timeout_range[1]) / 1000.0

    def _init_peers(self):
        """Initialize gRPC connections to peers"""
        for peer in self.peers:
            try:
                # CRITICAL FIX: Don't use ipv4: or dns: prefixes in Docker
                # Just use the hostname:port directly
                logger.info(f"Initializing connection to peer: {peer}")
                
                # Create channel with proper options for Docker networking
                channel = grpc.aio.insecure_channel(
                    peer,  # Direct format: "hostname:port"
                    options=[
                        # Keepalive settings
                        ('grpc.keepalive_time_ms', 30000),
                        ('grpc.keepalive_timeout_ms', 10000),
                        ('grpc.keepalive_permit_without_calls', 1),
                        ('grpc.http2.max_pings_without_data', 0),
                        # Enable initial connection
                        ('grpc.initial_reconnect_backoff_ms', 1000),
                        ('grpc.max_reconnect_backoff_ms', 5000),
                        # Connection settings
                        ('grpc.http2.min_time_between_pings_ms', 10000),
                        ('grpc.max_connection_idle_ms', 300000),
                        ('grpc.max_connection_age_ms', 600000),
                    ]
                )
                
                self.peer_channels[peer] = channel
                self.peer_stubs[peer] = raft_pb2_grpc.RaftServiceStub(channel)
                logger.info(f"✓ Successfully initialized connection to peer: {peer}")
                
            except Exception as e:
                logger.error(f"✗ Failed to initialize connection to {peer}: {e}", exc_info=True)

    def _get_peer_stub(self, peer: str):
        """Get or create peer stub with retry logic"""
        if peer not in self.peer_stubs or self.peer_stubs[peer] is None:
            try:
                logger.info(f"Reconnecting to peer: {peer}")
                
                channel = grpc.aio.insecure_channel(
                    peer,
                    options=[
                        ('grpc.keepalive_time_ms', 30000),
                        ('grpc.keepalive_timeout_ms', 10000),
                        ('grpc.keepalive_permit_without_calls', 1),
                        ('grpc.http2.max_pings_without_data', 0),
                        ('grpc.initial_reconnect_backoff_ms', 1000),
                        ('grpc.max_reconnect_backoff_ms', 5000),
                    ]
                )
                
                self.peer_channels[peer] = channel
                self.peer_stubs[peer] = raft_pb2_grpc.RaftServiceStub(channel)
                logger.info(f"✓ Reconnected to peer: {peer}")
                
            except Exception as e:
                logger.error(f"✗ Failed to reconnect to {peer}: {e}", exc_info=True)
                return None
                
        return self.peer_stubs.get(peer)

    async def start(self):
        """Start the Raft node"""
        self.running = True
        
        # Add delay before starting election timer to allow all nodes to start
        await asyncio.sleep(0.5)
        
        asyncio.create_task(self._election_timer())
        asyncio.create_task(self._heartbeat_sender())
        asyncio.create_task(self._log_applier())
        logger.info(f"🚀 Node {self.node_id} started as {self.state.value}")

    async def stop(self):
        """Stop the Raft node"""
        self.running = False
        
        # Close all gRPC channels
        for peer, channel in self.peer_channels.items():
            try:
                await channel.close()
                logger.debug(f"Closed channel to {peer}")
            except Exception as e:
                logger.error(f"Error closing channel to {peer}: {e}")
        
        logger.info(f"Node {self.node_id} stopped")

    async def _election_timer(self):
        """Monitor election timeout and trigger elections"""
        while self.running:
            await asyncio.sleep(0.05)  # Check every 50ms
            
            if self.state != NodeState.LEADER:
                elapsed = time.time() - self.last_heartbeat
                
                if elapsed >= self.election_timeout:
                    # Use pre-vote to reduce disruptions
                    if self.enable_prevote and not self.in_prevote:
                        await self._start_prevote()
                    else:
                        await self._start_election()

    async def _start_prevote(self):
        """Pre-vote phase to check if election would succeed"""
        self.in_prevote = True
        logger.info(f"Node {self.node_id} starting pre-vote for term {self.current_term + 1}")
        
        last_log_index = len(self.log)
        last_log_term = self.log[-1].term if self.log else 0
        
        # Count how many nodes would vote for us
        prevotes = 1  # Vote for self
        
        vote_tasks = []
        for peer in self.peers:
            task = asyncio.create_task(
                self._send_prevote(peer, last_log_index, last_log_term)
            )
            vote_tasks.append(task)
        
        results = await asyncio.gather(*vote_tasks, return_exceptions=True)
        
        # Count successful prevotes
        for i, result in enumerate(results):
            if isinstance(result, bool) and result:
                prevotes += 1
                logger.debug(f"Pre-vote granted by {self.peers[i]}")
            elif isinstance(result, Exception):
                logger.debug(f"Pre-vote to {self.peers[i]} failed: {result}")
        
        # If we would win, start real election
        if prevotes > (len(self.peers) + 1) / 2:
            logger.info(f"Node {self.node_id} pre-vote successful ({prevotes}/{len(self.peers)+1}), starting election")
            await self._start_election()
        else:
            logger.info(f"Node {self.node_id} pre-vote failed ({prevotes}/{len(self.peers)+1}), staying follower")
            self.in_prevote = False
            self.election_timeout = self._random_timeout()
            self.last_heartbeat = time.time()

    async def _send_prevote(self, peer: str, last_log_index: int, last_log_term: int) -> bool:
        """Send pre-vote request to peer"""
        try:
            stub = self._get_peer_stub(peer)
            if not stub:
                return False
            
            # Pre-vote uses term+1 but doesn't increment our term yet
            request = raft_pb2.RequestVoteRequest(
                term=self.current_term + 1,
                candidate_id=self.node_id,
                last_log_index=last_log_index,
                last_log_term=last_log_term
            )
            
            response = await stub.RequestVote(request, timeout=0.5)
            return response.vote_granted
            
        except grpc.aio.AioRpcError as e:
            logger.debug(f"Pre-vote gRPC error to {peer}: {e.code()} - {e.details()}")
            return False
        except Exception as e:
            logger.debug(f"Pre-vote request to {peer} failed: {type(e).__name__}: {e}")
            return False

    async def _start_election(self):
        """Start a new election"""
        async with self.lock:
            self.state = NodeState.CANDIDATE
            self.current_term += 1
            self.voted_for = self.node_id
            self.votes_received = {self.node_id}
            self.election_timeout = self._random_timeout()
            self.last_heartbeat = time.time()
            self.in_prevote = False
            
            logger.info(f"🗳️  Node {self.node_id} starting election for term {self.current_term}")

        last_log_index = len(self.log)
        last_log_term = self.log[-1].term if self.log else 0

        # Request votes from all peers
        vote_tasks = []
        for peer in self.peers:
            task = asyncio.create_task(
                self._request_vote(peer, last_log_index, last_log_term)
            )
            vote_tasks.append(task)

        await asyncio.gather(*vote_tasks, return_exceptions=True)

        # Check if we won the election
        async with self.lock:
            if self.state == NodeState.CANDIDATE and len(self.votes_received) > (len(self.peers) + 1) / 2:
                await self._become_leader()
            elif self.state == NodeState.CANDIDATE:
                logger.info(f"Node {self.node_id} lost election for term {self.current_term} ({len(self.votes_received)}/{len(self.peers) + 1} votes)")

    async def _request_vote(self, peer: str, last_log_index: int, last_log_term: int):
        """Request vote from a peer"""
        try:
            stub = self._get_peer_stub(peer)
            if not stub:
                logger.debug(f"No stub available for {peer}")
                return

            request = raft_pb2.RequestVoteRequest(
                term=self.current_term,
                candidate_id=self.node_id,
                last_log_index=last_log_index,
                last_log_term=last_log_term
            )

            response = await stub.RequestVote(request, timeout=0.5)
            
            async with self.lock:
                if response.term > self.current_term:
                    await self._become_follower(response.term)
                elif response.vote_granted and self.state == NodeState.CANDIDATE:
                    self.votes_received.add(peer)
                    logger.info(f"✓ Node {self.node_id} received vote from {peer}, total: {len(self.votes_received)}/{len(self.peers) + 1}")
                    
        except grpc.aio.AioRpcError as e:
            logger.debug(f"Vote request gRPC error to {peer}: {e.code()} - {e.details()}")
        except Exception as e:
            logger.debug(f"Failed to get vote from {peer}: {type(e).__name__}: {e}")

    async def _become_leader(self):
        """Transition to leader state"""
        logger.info(f"🎉 Node {self.node_id} became LEADER for term {self.current_term}")
        self.state = NodeState.LEADER
        self.current_leader = self.node_id
        
        # Initialize leader state
        self.next_index = {peer: len(self.log) + 1 for peer in self.peers}
        self.match_index = {peer: 0 for peer in self.peers}
        
        # Send immediate heartbeat to establish authority
        await self._send_heartbeats()

    async def _become_follower(self, term: int):
        """Transition to follower state"""
        if self.state == NodeState.LEADER:
            logger.info(f"Node {self.node_id} stepping down from LEADER to FOLLOWER for term {term}")
        else:
            logger.info(f"Node {self.node_id} became FOLLOWER for term {term}")
        
        self.state = NodeState.FOLLOWER
        self.current_term = term
        self.voted_for = None
        self.last_heartbeat = time.time()
        self.in_prevote = False

    async def _heartbeat_sender(self):
        """Send periodic heartbeats when leader"""
        while self.running:
            await asyncio.sleep(self.heartbeat_interval)
            
            if self.state == NodeState.LEADER:
                await self._send_heartbeats()

    async def _send_heartbeats(self):
        """Send AppendEntries (heartbeat) to all peers"""
        tasks = []
        for peer in self.peers:
            task = asyncio.create_task(self._replicate_to_peer(peer))
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Log any errors
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.debug(f"Heartbeat to {self.peers[i]} failed: {type(result).__name__}")

    async def _replicate_to_peer(self, peer: str):
        """Replicate log entries to a specific peer"""
        try:
            stub = self._get_peer_stub(peer)
            if not stub:
                return

            next_idx = self.next_index.get(peer, 1)
            prev_log_index = next_idx - 1
            prev_log_term = self.log[prev_log_index - 1].term if prev_log_index > 0 and prev_log_index <= len(self.log) else 0

            # Prepare entries to send
            entries = []
            if next_idx <= len(self.log):
                for entry in self.log[next_idx - 1:]:
                    entries.append(raft_pb2.LogEntry(
                        term=entry.term,
                        index=entry.index,
                        command=json.dumps({"ip": entry.ip, "timestamp": entry.timestamp}),
                        command_type=""
                    ))

            request = raft_pb2.AppendEntriesRequest(
                term=self.current_term,
                leader_id=self.node_id,
                prev_log_index=prev_log_index,
                prev_log_term=prev_log_term,
                entries=entries,
                leader_commit=self.commit_index
            )

            response = await stub.AppendEntries(request, timeout=0.5)

            async with self.lock:
                if response.term > self.current_term:
                    await self._become_follower(response.term)
                elif self.state == NodeState.LEADER:
                    if response.success:
                        # Update indices on success
                        if entries:
                            self.next_index[peer] = next_idx + len(entries)
                            self.match_index[peer] = next_idx + len(entries) - 1
                            logger.debug(f"Replicated {len(entries)} entries to {peer}")
                        await self._update_commit_index()
                    else:
                        # Decrement next_index on failure (log inconsistency)
                        self.next_index[peer] = max(1, self.next_index[peer] - 1)
                        logger.debug(f"Log inconsistency with {peer}, retrying with index {self.next_index[peer]}")
                        
        except grpc.aio.AioRpcError as e:
            logger.debug(f"Replication gRPC error to {peer}: {e.code()}")
        except Exception as e:
            logger.debug(f"Failed to replicate to {peer}: {type(e).__name__}: {e}")

    async def _update_commit_index(self):
        """Update commit index based on majority replication"""
        if self.state != NodeState.LEADER:
            return

        # Find highest N where majority of match_index[i] >= N
        for n in range(len(self.log), self.commit_index, -1):
            if self.log[n - 1].term != self.current_term:
                continue

            # Count replicas
            count = 1  # Leader has it
            for peer in self.peers:
                if self.match_index.get(peer, 0) >= n:
                    count += 1

            # Check if majority
            if count > (len(self.peers) + 1) / 2:
                self.commit_index = n
                logger.info(f"Updated commit_index to {n}")
                break

    async def _log_applier(self):
        """Apply committed log entries"""
        while self.running:
            await asyncio.sleep(0.05)
            
            if self.last_applied < self.commit_index:
                self.last_applied += 1
                entry = self.log[self.last_applied - 1]
                logger.debug(f"Applied entry {self.last_applied}: IP={entry.ip}")

    async def append_entry(self, ip: str, timestamp: float) -> bool:
        """Append a new entry to the log (leader only)"""
        async with self.lock:
            if self.state != NodeState.LEADER:
                logger.warning(f"Cannot append entry: not leader (current state: {self.state.value})")
                return False
            
            entry = LogEntry(
                term=self.current_term,
                index=len(self.log) + 1,
                ip=ip,
                timestamp=timestamp
            )
            self.log.append(entry)
            logger.info(f"Appended entry {entry.index} for IP {ip}")
            return True

    def is_leader(self) -> bool:
        """Check if this node is the leader"""
        return self.state == NodeState.LEADER

    def get_leader(self) -> Optional[str]:
        """Get the current leader node ID"""
        return self.current_leader

    def get_state(self) -> dict:
        """Get current node state for debugging"""
        return {
            "node_id": self.node_id,
            "state": self.state.value,
            "term": self.current_term,
            "leader": self.current_leader,
            "log_size": len(self.log),
            "commit_index": self.commit_index,
            "last_applied": self.last_applied,
            "peers": self.peers,
            "connected_peers": list(self.peer_stubs.keys())
        }


class RaftServicer(raft_pb2_grpc.RaftServiceServicer):
    """gRPC service implementation for Raft"""

    def __init__(self, raft_node: RaftNode):
        self.node = raft_node

    async def RequestVote(self, request, context):
        """Handle RequestVote RPC"""
        try:
            async with self.node.lock:
                # Update term if necessary
                if request.term > self.node.current_term:
                    await self.node._become_follower(request.term)

                vote_granted = False

                if request.term < self.node.current_term:
                    # Reject if term is outdated
                    vote_granted = False
                elif (self.node.voted_for is None or self.node.voted_for == request.candidate_id):
                    # Check if candidate's log is at least as up-to-date
                    last_log_index = len(self.node.log)
                    last_log_term = self.node.log[-1].term if self.node.log else 0

                    log_ok = (request.last_log_term > last_log_term or
                              (request.last_log_term == last_log_term and
                               request.last_log_index >= last_log_index))
                    
                    if log_ok:
                        self.node.voted_for = request.candidate_id
                        self.node.last_heartbeat = time.time()
                        vote_granted = True
                        logger.info(f"✓ Granted vote to {request.candidate_id} for term {request.term}")

                return raft_pb2.RequestVoteResponse(
                    term=self.node.current_term,
                    vote_granted=vote_granted
                )
        except Exception as e:
            logger.error(f"Error in RequestVote: {e}", exc_info=True)
            raise

    async def AppendEntries(self, request, context):
        """Handle AppendEntries RPC (heartbeat or log replication)"""
        try:
            async with self.node.lock:
                # Update term if necessary
                if request.term > self.node.current_term:
                    await self.node._become_follower(request.term)

                # Reset election timer (received heartbeat)
                self.node.last_heartbeat = time.time()
                self.node.current_leader = request.leader_id

                success = False
                
                if request.term < self.node.current_term:
                    # Reject if term is outdated
                    success = False
                else:
                    # Step down if we're not already a follower
                    if self.node.state != NodeState.FOLLOWER:
                        await self.node._become_follower(request.term)
                    
                    # Check log consistency
                    if request.prev_log_index == 0 or (
                        request.prev_log_index <= len(self.node.log) and
                        self.node.log[request.prev_log_index - 1].term == request.prev_log_term
                    ):
                        success = True
                        
                        # Append new entries
                        if request.entries:
                            # Delete conflicting entries and append new ones
                            self.node.log = self.node.log[:request.prev_log_index]
                            
                            for entry_pb in request.entries:
                                data = json.loads(entry_pb.command)
                                entry = LogEntry(
                                    term=entry_pb.term,
                                    index=entry_pb.index,
                                    ip=data["ip"],
                                    timestamp=data["timestamp"]
                                )
                                self.node.log.append(entry)
                            
                            logger.info(f"Appended {len(request.entries)} entries from leader {request.leader_id}")
                        
                        # Update commit index
                        if request.leader_commit > self.node.commit_index:
                            self.node.commit_index = min(
                                request.leader_commit,
                                len(self.node.log)
                            )
                            logger.debug(f"Updated commit_index to {self.node.commit_index}")

                return raft_pb2.AppendEntriesResponse(
                    term=self.node.current_term,
                    success=success,
                    conflict_index=0,
                    conflict_term=0
                )
        except Exception as e:
            logger.error(f"Error in AppendEntries: {e}", exc_info=True)
            raise

    async def InstallSnapshot(self, request, context):
        """Handle InstallSnapshot RPC (not implemented for simple version)"""
        return raft_pb2.InstallSnapshotResponse(term=self.node.current_term)