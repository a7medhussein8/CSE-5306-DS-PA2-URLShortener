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

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Basic Raft types
# ---------------------------------------------------------------------------

class NodeState(Enum):
    FOLLOWER = "FOLLOWER"
    CANDIDATE = "CANDIDATE"
    LEADER = "LEADER"


@dataclass
class LogEntry:
    term: int
    index: int
    command: str       # JSON string (rate-limit command, etc.)
    command_type: str  # e.g. "SET_LIMIT", "BLOCK_CLIENT", ...


# ---------------------------------------------------------------------------
# Raft Node
# ---------------------------------------------------------------------------

class RaftNode:
    """Simplified Raft implementation following the given specifications."""

    def __init__(self, node_id: str, peers: List[str]):
        """
        :param node_id: string ID of this node (e.g., "ratelimit-1:50051")
        :param peers:   list of peer addresses "host:port"
        """
        self.node_id = node_id
        self.peers = peers  # List of peer addresses (host:port)

        # ---------------------------
        # Persistent state on all servers
        # ---------------------------
        self.current_term: int = 0
        self.voted_for: Optional[str] = None
        self.log: List[LogEntry] = []

        # ---------------------------
        # Volatile state on all servers
        # ---------------------------
        self.commit_index: int = 0  # Highest log entry known to be committed
        self.last_applied: int = 0  # Highest log entry applied to state machine

        # ---------------------------
        # Volatile state on leaders
        # (reinitialized after election)
        # ---------------------------
        self.next_index: dict[str, int] = {}   # peer -> next log index to send
        self.match_index: dict[str, int] = {}  # peer -> highest replicated index

        # ---------------------------
        # Node state
        # ---------------------------
        self.state: NodeState = NodeState.FOLLOWER
        self.current_leader: Optional[str] = None

        # ---------------------------
        # Timers
        # ---------------------------
        self.heartbeat_interval: float = 1.0  # seconds between heartbeats
        self.election_timeout: float = self._random_election_timeout()
        self.last_heartbeat: float = time.time()

        # ---------------------------
        # gRPC connections to peers
        # ---------------------------
        self.peer_stubs: dict[str, raft_pb2_grpc.RaftServiceStub] = {}
        self.peer_channels: dict[str, grpc.aio.Channel] = {}

        # Ensure each node has a unique RNG stream
        random.seed(f"{node_id}{time.time()}".encode())

        # Initialize gRPC channels/stubs (non-blocking)
        self._init_peers()

        # ---------------------------
        # Concurrency helpers
        # ---------------------------
        self.running: bool = False
        self.lock = asyncio.Lock()

        logger.info(f"✓ Node {self.node_id} initialized")
        logger.info(f"  - State: {self.state.value}")
        logger.info(f"  - Heartbeat interval: {self.heartbeat_interval}s")
        logger.info(f"  - Election timeout: {self.election_timeout:.2f}s")
        logger.info(f"  - Peers: {self.peers}")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _random_election_timeout(self) -> float:
        """Random election timeout between 1.5 and 3.0 seconds."""
        return random.uniform(1.5, 3.0)

    def _init_peers(self) -> None:
        """Create gRPC channels/stubs for peers (no blocking connection)."""
        for peer in self.peers:
            try:
                channel = grpc.aio.insecure_channel(
                    peer,
                    options=[
                        ("grpc.keepalive_time_ms", 100000),
                        ("grpc.keepalive_timeout_ms", 500000),
                        ("grpc.keepalive_permit_without_calls", True),
                        ("grpc.http2.max_pings_without_data", 0),
                    ],
                )
                self.peer_channels[peer] = channel
                self.peer_stubs[peer] = raft_pb2_grpc.RaftServiceStub(channel)
                logger.info(f"✓ Node {self.node_id} created channel to peer {peer}")
            except Exception as e:
                logger.error(f"✗ Node {self.node_id} failed to init channel to {peer}: {e}")

    def _get_peer_stub(self, peer: str) -> Optional[raft_pb2_grpc.RaftServiceStub]:
        """Get or recreate peer stub if needed."""
        if peer not in self.peer_stubs or self.peer_stubs[peer] is None:
            try:
                channel = grpc.aio.insecure_channel(
                    peer,
                    options=[
                        ("grpc.keepalive_time_ms", 1000000),
                        ("grpc.keepalive_timeout_ms", 50000000),
                        ("grpc.keepalive_permit_without_calls", True),
                        ("grpc.http2.max_pings_without_data", 0),
                    ],
                )
                self.peer_channels[peer] = channel
                self.peer_stubs[peer] = raft_pb2_grpc.RaftServiceStub(channel)
                logger.info(f"✓ Node {self.node_id} re-created channel to peer {peer}")
            except Exception as e:
                logger.error(f"✗ Node {self.node_id} failed to reconnect to {peer}: {e}")
                return None
        return self.peer_stubs.get(peer)


    async def _wait_for_peers(self) -> None:
        """
        Best-effort connectivity check to peers.

        Runs in the background and DOES NOT block the node from starting
        elections/heartbeats. This avoids all nodes deadlocking on startup if
        one peer is down or slow.
        """
        logger.info(f"Node {self.node_id} waiting for peers in background...")

        for peer in self.peers:
            while self.running:
                try:
                    stub = self._get_peer_stub(peer)
                    if stub is None:
                        await asyncio.sleep(0.5)
                        continue

                    # Term=0 is used only as a "ping". The server side
                    # immediately returns without changing state.
                    request = raft_pb2.RequestVoteRequest(
                        term=0,
                        candidate_id=self.node_id,
                        last_log_index=0,
                        last_log_term=0,
                    )
                    await asyncio.wait_for(stub.RequestVote(request), timeout=1.0)
                    logger.info(f"Node {self.node_id} connectivity OK to {peer}")
                    break
                except Exception as e:
                    logger.error(
                        f"Node {self.node_id} peer check to {peer} failed: {e}"
                    )
                    await asyncio.sleep(0.5)

            if not self.running:
                logger.info(
                    f"Node {self.node_id} stopped while still waiting for peers"
                )
                return

        logger.info(f"Node {self.node_id} connectivity checks finished")

    async def start(self) -> None:
        """Start the Raft node (background timers + best-effort peer checks)."""
        self.running = True
        self.state = NodeState.FOLLOWER

        # ✅ Do NOT block startup on peer connectivity; run this in background.
        asyncio.create_task(self._wait_for_peers())

        # Background tasks for Raft behavior
        asyncio.create_task(self._election_timer())
        asyncio.create_task(self._heartbeat_sender())
        asyncio.create_task(self._log_applier())

        logger.info(f"🚀 Node {self.node_id} started as {self.state.value}")

    async def stop(self) -> None:
        """Stop the Raft node and close channels."""
        self.running = False

        for peer, channel in self.peer_channels.items():
            try:
                await channel.close()
            except Exception as e:
                logger.error(f"Error closing channel to {peer}: {e}")

        logger.info(f"Node {self.node_id} stopped")

    # -----------------------------------------------------------------------
    # Election logic
    # -----------------------------------------------------------------------

    async def _election_timer(self) -> None:
        """Monitor election timeout and start an election if needed."""
        while self.running:
            await asyncio.sleep(0.1)  # Check every 100ms

            if self.state != NodeState.LEADER:
                elapsed = time.time() - self.last_heartbeat
                if elapsed >= self.election_timeout:
                    logger.info(
                        f"⏰ Node {self.node_id} election timeout "
                        f"({elapsed:.2f}s >= {self.election_timeout:.2f}s)"
                    )
                    await self._start_election()

    async def _start_election(self) -> None:
        """Start a new election (transition to CANDIDATE)."""
        async with self.lock:
            self.state = NodeState.CANDIDATE
            self.current_term += 1
            self.voted_for = self.node_id
            # self.election_timeout = self._random_election_timeout()
            self.last_heartbeat = time.time()

            current_term = self.current_term
            votes_received = 1  # vote for self

            logger.info("")
            logger.info("=" * 60)
            logger.info(
                f"🗳️  Node {self.node_id} starting ELECTION for term {current_term}"
            )
            logger.info("=" * 60)

        last_log_index = len(self.log)
        last_log_term = self.log[-1].term if self.log else 0

        # Send RequestVote to all peers
        vote_tasks = [
            asyncio.create_task(
                self._request_vote(peer, current_term, last_log_index, last_log_term)
            )
            for peer in self.peers
        ]

        results = await asyncio.gather(*vote_tasks, return_exceptions=True)
        # results =  [T, T, F ,F]
        for result in results:
            if isinstance(result, bool) and result:
                votes_received += 1

        async with self.lock:
            majority = (len(self.peers) + 1) // 2 + 1

            logger.info("")
            logger.info(
                f"📊 Node {self.node_id} election results: "
                f"{votes_received}/{len(self.peers) + 1} votes (need {majority})"
            )

            if self.state == NodeState.CANDIDATE and self.current_term == current_term:
                if votes_received >= majority:
                    await self._become_leader()
                else:
                    logger.info(
                        f"❌ Node {self.node_id} lost election for term {current_term}"
                    )
                    self.state = NodeState.FOLLOWER
                    # DO NOT decrement term - terms never go backwards in Raft
                    self.voted_for = None

    async def _request_vote(
        self,
        peer: str,
        term: int,
        last_log_index: int,
        last_log_term: int,
    ) -> bool:
        """Send RequestVote RPC to a peer."""
        try:
            stub = self._get_peer_stub(peer)
            if not stub:
                return False

            logger.info(
                f"📤 Node {self.node_id} sends RPC RequestVote to Node {peer} "
                f"(term={term})"
            )

            request = raft_pb2.RequestVoteRequest(
                term=term,
                candidate_id=self.node_id,
                last_log_index=last_log_index,
                last_log_term=last_log_term,
            )

            response = await stub.RequestVote(request)

            async with self.lock:
                # if response.term > self.current_term:
                #     logger.warning(
                #         f"⚠️  Node {self.node_id} saw higher term "
                #         f"{response.term} from {peer}, stepping down"
                #     )
                #     await self._become_follower(response.term)
                #     return False

                if response.vote_granted:
                    logger.info(
                        f"✅ Node {self.node_id} received vote from {peer}"
                    )
                    return True
                else:
                    logger.info(
                        f"❌ Node {self.node_id} vote denied by {peer}"
                    )
                    return False

        except Exception as e:
            logger.debug(f"Failed to get vote from {peer}: {e}")
            return False

    async def _become_leader(self) -> None:
        """Transition to LEADER state."""
        self.state = NodeState.LEADER
        self.current_leader = self.node_id

        # Initialize leader state
        self.next_index = {peer: len(self.log) + 1 for peer in self.peers}
        self.match_index = {peer: 0 for peer in self.peers}

        logger.info("")
        logger.info("=" * 60)
        logger.info(
            f"👑 Node {self.node_id} became LEADER for term {self.current_term}"
        )
        logger.info("=" * 60)
        logger.info("")

        # Send immediate heartbeat
        await self._send_heartbeats()

    async def _become_follower(self, term: int) -> None:
        """Transition to FOLLOWER state."""
        old_state = self.state
        self.state = NodeState.FOLLOWER
        self.current_term = max(self.current_term, term)
        self.voted_for = None
        self.last_heartbeat = time.time()

        if old_state == NodeState.LEADER:
            logger.info(
                f"👇 Node {self.node_id} stepping down from LEADER to FOLLOWER "
                f"(term {term})"
            )
        else:
            logger.info(f"👤 Node {self.node_id} became FOLLOWER (term {term})")

    # -----------------------------------------------------------------------
    # Heartbeats / log replication
    # -----------------------------------------------------------------------

    async def _heartbeat_sender(self) -> None:
        """Periodically send heartbeats if this node is the leader."""
        while self.running:
            await asyncio.sleep(self.heartbeat_interval)
            if self.state == NodeState.LEADER:
                logger.info("1💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓")
                await self._send_heartbeats()

    async def _send_heartbeats(self) -> None:
        """Send AppendEntries (heartbeat/log replication) to all peers."""
        if self.state != NodeState.LEADER:
            return

        logger.info(f"💓 Node {self.node_id} sending heartbeats to all peers")
        logger.info("2💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓")
        tasks = [
            asyncio.create_task(self._replicate_to_peer(peer))
            for peer in self.peers
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("3💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓")

    async def _replicate_to_peer(self, peer: str) -> None:
        """Replicate log entries or send heartbeats to a single peer."""
        try:

            if self.state != NodeState.LEADER:
                return
            
            stub = self._get_peer_stub(peer)
            if not stub:
                return

            next_idx = self.next_index.get(peer, 1)
            prev_log_index = next_idx - 1
            prev_log_term = 0

            if 0 < prev_log_index <= len(self.log):
                prev_log_term = self.log[prev_log_index - 1].term

            entries = []
            if next_idx <= len(self.log):
                for entry in self.log[next_idx - 1 :]:
                    entries.append(
                        raft_pb2.LogEntry(
                            term=entry.term,
                            index=entry.index,
                            command=entry.command,
                            command_type=entry.command_type,
                        )
                    )

            if entries:
                logger.info(
                    f"📤 Node {self.node_id} sends RPC AppendEntries to Node {peer} "
                    f"({len(entries)} entries)"
                )
            else:
                logger.info(
                    f"📤 Node {self.node_id} sends RPC AppendEntries (heartbeat) "
                    f"to Node {peer}"
                )
            logger.info("4💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓")
            request = raft_pb2.AppendEntriesRequest(
                term=self.current_term,
                leader_id=self.node_id,
                prev_log_index=prev_log_index,
                prev_log_term=prev_log_term,
                entries=entries,
                leader_commit=self.commit_index,
            )

            response = await stub.AppendEntries(request)
            logger.info("5💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓")
            # async with self.lock:
                # if response.term > self.current_term:
                #     logger.warning(
                #         f"⚠️  Leader {self.node_id} stepping down: "
                #         f"{peer} has higher term {response.term}"
                #     )
                #     await self._become_follower(response.term)
                #     return

               

            if response.success:
                logger.info("6💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓")
                if entries:
                    logger.info(
                        f"✅ Leader {self.node_id} received ACK from follower "
                        f"{peer} - replicated {len(entries)} entries"
                    )
                    self.next_index[peer] = next_idx + len(entries)
                    self.match_index[peer] = next_idx + len(entries) - 1
                else:
                    logger.info(
                        f"✅ Leader {self.node_id} received heartbeat ACK "
                        f"from follower {peer}"
                    )
                # await self._update_commit_index()
            else:
                logger.info("7💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓💓")
                # log inconsistency, back off and retry
                self.next_index[peer] = max(1, self.next_index.get(peer, 1) - 1)
                logger.warning(
                    f"⚠️  Log inconsistency with {peer}, "
                    f"retrying with index {self.next_index[peer]}"
                )

        except Exception as e:
            logger.debug(f"Failed to replicate to {peer}: {e}")

    async def _update_commit_index(self) -> None:
        """Update commit index based on majority replication."""
        if self.state != NodeState.LEADER:
            return

        for n in range(len(self.log), self.commit_index, -1):
            if n == 0:
                break

            # Only commit entries from current term
            if self.log[n - 1].term != self.current_term:
                continue

            count = 1  # Leader has it
            for peer in self.peers:
                if self.match_index.get(peer, 0) >= n:
                    count += 1

            majority = (len(self.peers) + 1) // 2 + 1
            if count >= majority:
                old_commit = self.commit_index
                self.commit_index = n
                logger.info(
                    f"📝 Leader {self.node_id} committed entries up to index {n} "
                    f"(majority: {count}/{len(self.peers) + 1}) "
                    f"(was {old_commit})"
                )
                break

    async def _log_applier(self) -> None:
        """Apply committed log entries to the local state machine."""
        while self.running:
            await asyncio.sleep(0.1)

            while self.last_applied < self.commit_index:
                self.last_applied += 1
                entry = self.log[self.last_applied - 1]
                logger.info(
                    f"✓ Node {self.node_id} applied entry {self.last_applied}: "
                    f"{entry.command_type}"
                )

    # -----------------------------------------------------------------------
    # Public helpers used by the rate limit service
    # -----------------------------------------------------------------------

    async def append_entry(self, command: str, command_type: str) -> bool:
        """
        Append a new entry to the log (leader only).
        Returns True if successfully appended on leader, False otherwise.
        """
        async with self.lock:
            if self.state != NodeState.LEADER:
                logger.warning(
                    f"❌ Node {self.node_id} cannot append entry: "
                    f"not leader (state: {self.state.value})"
                )
                return False

            entry = LogEntry(
                term=self.current_term,
                index=len(self.log) + 1,
                command=command,
                command_type=command_type,
            )
            self.log.append(entry)

            logger.info(
                f"📝 Leader {self.node_id} appended entry {entry.index}: "
                f"{command_type}"
            )

            asyncio.create_task(self._send_heartbeats())
            return True

    def is_leader(self) -> bool:
        """Return True if this node is currently the leader."""
        return self.state == NodeState.LEADER

    def get_leader(self) -> Optional[str]:
        """Return the node_id of the current leader (if known)."""
        return self.current_leader

    def get_state(self) -> dict:
        """Return a snapshot of node state for debugging."""
        return {
            "node_id": self.node_id,
            "state": self.state.value,
            "term": self.current_term,
            "leader": self.current_leader,
            "log_size": len(self.log),
            "commit_index": self.commit_index,
            "last_applied": self.last_applied,
            "peers": self.peers,
        }


# ---------------------------------------------------------------------------
# gRPC Servicer
# ---------------------------------------------------------------------------

class RaftServicer(raft_pb2_grpc.RaftServiceServicer):
    """gRPC service implementation that delegates to a RaftNode."""

    def __init__(self, raft_node: RaftNode):
        self.node = raft_node

    # ------------------------ RequestVote ------------------------

    async def RequestVote(self, request, context):
        """Handle RequestVote RPC."""
        try:
           
            self.node.last_heartbeat = time.time()
            # Special case: term=0 is used as a connectivity ping
            if request.term == 0:
                return raft_pb2.RequestVoteResponse(
                    term=self.node.current_term,
                    vote_granted=False,
                )
            
            logger.info(
                f"📥 Node {self.node.node_id} runs RPC RequestVote "
                f"called by Node {request.candidate_id}"
            )

            async with self.node.lock:
               
                vote_granted = False

                if request.term < self.node.current_term:
                    
                    logger.info(
                        f"❌ Node {self.node.node_id} rejects vote for "
                        f"{request.candidate_id} "
                        f"(old term {request.term} < {self.node.current_term})"
                    )
                    vote_granted = False
                    return raft_pb2.RequestVoteResponse(
                        term=self.node.current_term,
                        vote_granted=False,
                    )
                elif request.term >= self.node.current_term:
                    
                    last_log_index = len(self.node.log)
                    last_log_term = (
                        self.node.log[-1].term if self.node.log else 0
                    )
                    
                    log_ok = (
                        request.last_log_term > last_log_term
                        or (
                            request.last_log_term == last_log_term
                            and request.last_log_index >= last_log_index
                        )
                    )
                    log_ok = True
                   
                    can_vote = (
                        self.node.voted_for is None
                        or self.node.voted_for == request.candidate_id
                    )
                    can_vote = True
                    if log_ok and can_vote:
                
                        self.node.voted_for = request.candidate_id
                        self.node.last_heartbeat = time.time()
                        vote_granted = True
                        logger.info(
                            f"✅ Node {self.node.node_id} grants vote to "
                            f"{request.candidate_id} for term {request.term}"
                        )
                        return raft_pb2.RequestVoteResponse(
                            term=self.node.current_term,
                            vote_granted=True,
                        )
                    else:
                        
                        logger.info(
                            f"❌ Node {self.node.node_id} rejects vote for "
                            f"{request.candidate_id} (log not up-to-date)"
                        )
                        return raft_pb2.RequestVoteResponse(
                            term=self.node.current_term,
                            vote_granted=False,
                        )
                

        except Exception as e:
            logger.error(f"Error in RequestVote: {e}", exc_info=True)
            raise

    # ------------------------ AppendEntries ------------------------

    async def AppendEntries(self, request, context):
        """Handle AppendEntries RPC (heartbeat or log replication)."""
        logger.info(f"📥 ****Node {self.node.node_id} runs RPC AppendEntries ***************" )
    

        try:
            if request.entries:
                logger.info(
                    f"📥 Node {self.node.node_id} runs RPC AppendEntries "
                    f"called by Leader {request.leader_id} "
                    f"({len(request.entries)} entries)"
                )
            else:
                logger.debug(
                    f"📥 Node {self.node.node_id} runs RPC AppendEntries "
                    f"(heartbeat) called by Leader {request.leader_id}"
                )

            async with self.node.lock:
                success = False

                await self.node._become_follower(request.term)

                # Valid heartbeat → reset election timer
                self.node.last_heartbeat = time.time()
                self.node.current_leader = request.leader_id
                success = True
                
                return raft_pb2.AppendEntriesResponse(
                    term=self.node.current_term,
                    success=True,
                    conflict_index=0,
                    conflict_term=0,
                )

        except Exception as e:
            logger.error(f"Error in AppendEntries: {e}", exc_info=True)
            raise

    # ------------------------ InstallSnapshot ------------------------

    async def InstallSnapshot(self, request, context):
        """Handle InstallSnapshot RPC (not implemented)."""
        return raft_pb2.InstallSnapshotResponse(term=self.node.current_term, ACK=True)
