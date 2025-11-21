#!/usr/bin/env python3
"""
Simple gRPC Call Tester
Makes actual RequestVote and AppendEntries calls to test gRPC connectivity
"""

import asyncio
import sys
import grpc

try:
    import raft_pb2
    import raft_pb2_grpc
except ImportError:
    print("ERROR: raft_pb2 not found. Make sure you're running this inside a container.")
    sys.exit(1)

async def test_request_vote(target):
    """Test RequestVote RPC call"""
    print(f"\n{'='*60}")
    print(f"Testing RequestVote to: {target}")
    print(f"{'='*60}")
    
    try:
        # Create channel
        print(f"1. Creating gRPC channel to {target}...")
        channel = grpc.aio.insecure_channel(target)
        stub = raft_pb2_grpc.RaftServiceStub(channel)
        
        # Create request
        print("2. Creating RequestVote request...")
        request = raft_pb2.RequestVoteRequest(
            term=999,  # Test term
            candidate_id="test-client",
            last_log_index=0,
            last_log_term=0
        )
        
        # Make the call
        print("3. Sending RequestVote RPC...")
        response = await asyncio.wait_for(
            stub.RequestVote(request),
            timeout=5.0
        )
        
        # Display results
        print(f"\n✓ SUCCESS!")
        print(f"  Response term: {response.term}")
        print(f"  Vote granted: {response.vote_granted}")
        
        await channel.close()
        return True
        
    except asyncio.TimeoutError:
        print("\n✗ FAILED: Request timed out after 5 seconds")
        return False
    except grpc.aio.AioRpcError as e:
        print(f"\n✗ FAILED: gRPC Error")
        print(f"  Code: {e.code()}")
        print(f"  Details: {e.details()}")
        return False
    except Exception as e:
        print(f"\n✗ FAILED: {type(e).__name__}: {e}")
        return False

async def test_append_entries(target):
    """Test AppendEntries RPC call (heartbeat)"""
    print(f"\n{'='*60}")
    print(f"Testing AppendEntries (heartbeat) to: {target}")
    print(f"{'='*60}")
    
    try:
        # Create channel
        print(f"1. Creating gRPC channel to {target}...")
        channel = grpc.aio.insecure_channel(target)
        stub = raft_pb2_grpc.RaftServiceStub(channel)
        
        # Create request (empty entries = heartbeat)
        print("2. Creating AppendEntries request...")
        request = raft_pb2.AppendEntriesRequest(
            term=999,
            leader_id="test-client",
            prev_log_index=0,
            prev_log_term=0,
            entries=[],  # Empty = heartbeat
            leader_commit=0
        )
        
        # Make the call
        print("3. Sending AppendEntries RPC...")
        response = await asyncio.wait_for(
            stub.AppendEntries(request),
            timeout=5.0
        )
        
        # Display results
        print(f"\n✓ SUCCESS!")
        print(f"  Response term: {response.term}")
        print(f"  Success: {response.success}")
        
        await channel.close()
        return True
        
    except asyncio.TimeoutError:
        print("\n✗ FAILED: Request timed out after 5 seconds")
        return False
    except grpc.aio.AioRpcError as e:
        print(f"\n✗ FAILED: gRPC Error")
        print(f"  Code: {e.code()}")
        print(f"  Details: {e.details()}")
        return False
    except Exception as e:
        print(f"\n✗ FAILED: {type(e).__name__}: {e}")
        return False

async def main():
    """Main test routine"""
    print("\n" + "="*60)
    print("  Raft gRPC Call Tester")
    print("="*60)
    
    # Get targets from command line or use defaults
    if len(sys.argv) > 1:
        targets = sys.argv[1:]
    else:
        # Default targets
        targets = [
            "ratelimit-2:50051",
            "ratelimit-3:50051",
            "ratelimit-4:50051",
            "ratelimit-5:50051"
        ]
    
    print(f"\nTesting {len(targets)} peers:")
    for target in targets:
        print(f"  - {target}")
    
    # Test each target
    results = []
    for target in targets:
        # Test RequestVote
        vote_ok = await test_request_vote(target)
        
        # Test AppendEntries
        append_ok = await test_append_entries(target)
        
        results.append({
            'target': target,
            'vote': vote_ok,
            'append': append_ok
        })
        
        await asyncio.sleep(0.5)  # Small delay between tests
    
    # Summary
    print(f"\n{'='*60}")
    print("  Summary")
    print(f"{'='*60}\n")
    
    for result in results:
        vote_status = "✓ PASS" if result['vote'] else "✗ FAIL"
        append_status = "✓ PASS" if result['append'] else "✗ FAIL"
        
        print(f"{result['target']}:")
        print(f"  RequestVote:   {vote_status}")
        print(f"  AppendEntries: {append_status}")
        print()
    
    # Final verdict
    all_passed = all(r['vote'] and r['append'] for r in results)
    if all_passed:
        print("✓ All gRPC calls SUCCEEDED!")
        print("  Your Raft cluster gRPC connectivity is working correctly.")
    else:
        failed = sum(1 for r in results if not (r['vote'] and r['append']))
        print(f"✗ {failed}/{len(results)} peers FAILED")
        print("  gRPC connectivity has issues. Check:")
        print("    1. Are all containers running?")
        print("    2. Are they on the same Docker network?")
        print("    3. Is the gRPC server listening on port 50051?")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)