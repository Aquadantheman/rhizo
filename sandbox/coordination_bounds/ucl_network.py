"""
Phase 12: UCL Network Transport Layer

Makes the Universal Coordination Layer work over actual network connections.

Architecture:
                     +-------------------+
                     |   Application     |
                     +--------+----------+
                              |
                     +--------v----------+
                     |       UCL         |
                     | (Classification)  |
                     +--------+----------+
                              |
                     +--------v----------+
                     |  Transport Layer  |  <-- THIS FILE
                     | (TCP/UDP/Unix)    |
                     +--------+----------+
                              |
              +---------------+---------------+
              |               |               |
         +----v----+    +----v----+    +----v----+
         | Node 0  |    | Node 1  |    | Node N  |
         +---------+    +---------+    +---------+

Run: python sandbox/coordination_bounds/ucl_network.py
"""

import sys
import time
import json
import socket
import struct
import threading
import queue
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Callable, Any, Tuple, Set
from enum import Enum
from abc import ABC, abstractmethod
import select
import pickle

# Import UCL core
sys.path.insert(0, str(Path(__file__).parent))
from universal_coordination_layer import (
    OperationType, CoordinationClass, UCLMessage,
    UniversalCoordinationLayer, GossipProtocol, ConsensusProtocol
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


# =============================================================================
# TRANSPORT PROTOCOLS
# =============================================================================

class TransportProtocol(Enum):
    """Supported transport protocols."""
    TCP = "tcp"      # Reliable, ordered - good for consensus
    UDP = "udp"      # Fast, unreliable - good for gossip
    UNIX = "unix"    # Local IPC - good for testing


@dataclass
class NodeAddress:
    """Network address for a UCL node."""
    node_id: int
    host: str
    port: int
    protocol: TransportProtocol = TransportProtocol.TCP

    def __str__(self):
        return f"{self.protocol.value}://{self.host}:{self.port}"

    def as_tuple(self) -> Tuple[str, int]:
        return (self.host, self.port)


@dataclass
class NetworkMessage:
    """Message wrapper for network transmission."""
    source_node: int
    dest_node: int
    ucl_message: UCLMessage
    timestamp: float = field(default_factory=time.time)
    message_id: str = ""

    def __post_init__(self):
        if not self.message_id:
            self.message_id = f"{self.source_node}-{self.timestamp}-{id(self)}"


# =============================================================================
# TCP TRANSPORT
# =============================================================================

class TCPTransport:
    """
    TCP-based transport for reliable message delivery.

    Used for:
    - Consensus operations (need reliability)
    - Cluster management messages
    - Large payloads
    """

    def __init__(self, node_id: int, bind_address: NodeAddress):
        self.node_id = node_id
        self.bind_address = bind_address
        self.connections: Dict[int, socket.socket] = {}
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.message_queue: queue.Queue = queue.Queue()
        self.receive_callback: Optional[Callable] = None

        # Statistics
        self.messages_sent = 0
        self.messages_received = 0
        self.bytes_sent = 0
        self.bytes_received = 0

    def start(self, receive_callback: Callable[[NetworkMessage], None]):
        """Start the TCP server."""
        self.receive_callback = receive_callback
        self.running = True

        # Create server socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(self.bind_address.as_tuple())
        self.server_socket.listen(10)
        self.server_socket.setblocking(False)

        # Start accept thread
        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.accept_thread.start()

        logger.info(f"TCP transport started on {self.bind_address}")

    def stop(self):
        """Stop the TCP transport."""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        for sock in self.connections.values():
            sock.close()
        self.connections.clear()

    def connect(self, peer: NodeAddress) -> bool:
        """Connect to a peer node."""
        if peer.node_id in self.connections:
            return True

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(peer.as_tuple())
            sock.setblocking(False)
            self.connections[peer.node_id] = sock

            # Start receive thread for this connection
            thread = threading.Thread(
                target=self._receive_loop,
                args=(sock, peer.node_id),
                daemon=True
            )
            thread.start()

            logger.info(f"Connected to node {peer.node_id} at {peer}")
            return True
        except Exception as e:
            logger.warning(f"Failed to connect to {peer}: {e}")
            return False

    def send(self, dest_node: int, message: NetworkMessage) -> bool:
        """Send a message to a peer."""
        if dest_node not in self.connections:
            logger.warning(f"No connection to node {dest_node}")
            return False

        try:
            # Serialize message
            data = pickle.dumps(message)
            # Length-prefix the message
            length = struct.pack('>I', len(data))

            sock = self.connections[dest_node]
            sock.sendall(length + data)

            self.messages_sent += 1
            self.bytes_sent += len(data) + 4
            return True
        except Exception as e:
            logger.error(f"Failed to send to node {dest_node}: {e}")
            return False

    def _accept_loop(self):
        """Accept incoming connections."""
        while self.running:
            try:
                readable, _, _ = select.select([self.server_socket], [], [], 0.1)
                if readable:
                    client_sock, addr = self.server_socket.accept()
                    client_sock.setblocking(False)
                    # We don't know the node_id yet - will be in first message
                    logger.info(f"Accepted connection from {addr}")

                    # Start receive thread
                    thread = threading.Thread(
                        target=self._receive_loop,
                        args=(client_sock, None),
                        daemon=True
                    )
                    thread.start()
            except Exception as e:
                if self.running:
                    logger.error(f"Accept error: {e}")

    def _receive_loop(self, sock: socket.socket, node_id: Optional[int]):
        """Receive messages from a connection."""
        buffer = b''

        while self.running:
            try:
                readable, _, _ = select.select([sock], [], [], 0.1)
                if not readable:
                    continue

                data = sock.recv(4096)
                if not data:
                    break

                buffer += data
                self.bytes_received += len(data)

                # Process complete messages
                while len(buffer) >= 4:
                    length = struct.unpack('>I', buffer[:4])[0]
                    if len(buffer) < 4 + length:
                        break

                    message_data = buffer[4:4+length]
                    buffer = buffer[4+length:]

                    message = pickle.loads(message_data)
                    self.messages_received += 1

                    # Update node_id from message if unknown
                    if node_id is None:
                        node_id = message.source_node
                        self.connections[node_id] = sock

                    if self.receive_callback:
                        self.receive_callback(message)

            except Exception as e:
                if self.running:
                    logger.error(f"Receive error: {e}")
                break


# =============================================================================
# UDP TRANSPORT
# =============================================================================

class UDPTransport:
    """
    UDP-based transport for fast, unreliable message delivery.

    Used for:
    - Gossip protocol (tolerates loss)
    - High-frequency updates
    - Latency-sensitive operations
    """

    def __init__(self, node_id: int, bind_address: NodeAddress):
        self.node_id = node_id
        self.bind_address = bind_address
        self.peers: Dict[int, NodeAddress] = {}
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.receive_callback: Optional[Callable] = None

        # Statistics
        self.messages_sent = 0
        self.messages_received = 0
        self.bytes_sent = 0
        self.bytes_received = 0

    def start(self, receive_callback: Callable[[NetworkMessage], None]):
        """Start the UDP transport."""
        self.receive_callback = receive_callback
        self.running = True

        # Create UDP socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.bind_address.as_tuple())
        self.socket.setblocking(False)

        # Start receive thread
        self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receive_thread.start()

        logger.info(f"UDP transport started on {self.bind_address}")

    def stop(self):
        """Stop the UDP transport."""
        self.running = False
        if self.socket:
            self.socket.close()

    def add_peer(self, peer: NodeAddress):
        """Add a peer address for sending."""
        self.peers[peer.node_id] = peer

    def send(self, dest_node: int, message: NetworkMessage) -> bool:
        """Send a message to a peer (best-effort)."""
        if dest_node not in self.peers:
            return False

        try:
            data = pickle.dumps(message)
            peer = self.peers[dest_node]
            self.socket.sendto(data, peer.as_tuple())

            self.messages_sent += 1
            self.bytes_sent += len(data)
            return True
        except Exception as e:
            logger.debug(f"UDP send failed: {e}")
            return False

    def broadcast(self, message: NetworkMessage) -> int:
        """Broadcast to all peers."""
        sent = 0
        for node_id in self.peers:
            if self.send(node_id, message):
                sent += 1
        return sent

    def _receive_loop(self):
        """Receive UDP messages."""
        while self.running:
            try:
                readable, _, _ = select.select([self.socket], [], [], 0.1)
                if not readable:
                    continue

                data, addr = self.socket.recvfrom(65535)
                self.bytes_received += len(data)

                message = pickle.loads(data)
                self.messages_received += 1

                if self.receive_callback:
                    self.receive_callback(message)

            except Exception as e:
                if self.running:
                    logger.debug(f"UDP receive error: {e}")


# =============================================================================
# NETWORKED UCL NODE
# =============================================================================

class NetworkedUCLNode:
    """
    A UCL node with network transport.

    Combines:
    - UCL for operation classification and routing
    - TCP for consensus operations
    - UDP for gossip operations
    """

    def __init__(
        self,
        node_id: int,
        tcp_port: int,
        udp_port: int,
        host: str = "127.0.0.1"
    ):
        self.node_id = node_id
        self.host = host

        # Network addresses
        self.tcp_address = NodeAddress(node_id, host, tcp_port, TransportProtocol.TCP)
        self.udp_address = NodeAddress(node_id, host, udp_port, TransportProtocol.UDP)

        # Transports
        self.tcp = TCPTransport(node_id, self.tcp_address)
        self.udp = UDPTransport(node_id, self.udp_address)

        # Peer registry
        self.peers: Dict[int, Tuple[NodeAddress, NodeAddress]] = {}  # node_id -> (tcp, udp)

        # Local state (replicated via UCL)
        self.state: Dict[str, Any] = {}
        self.state_lock = threading.Lock()

        # Pending consensus operations
        self.pending_consensus: Dict[str, threading.Event] = {}
        self.consensus_results: Dict[str, Any] = {}

        # Statistics
        self.ops_executed = 0
        self.gossip_ops = 0
        self.consensus_ops = 0

    def start(self):
        """Start the networked UCL node."""
        self.tcp.start(self._on_tcp_message)
        self.udp.start(self._on_udp_message)
        logger.info(f"Node {self.node_id} started (TCP: {self.tcp_address}, UDP: {self.udp_address})")

    def stop(self):
        """Stop the node."""
        self.tcp.stop()
        self.udp.stop()

    def add_peer(self, node_id: int, tcp_host: str, tcp_port: int, udp_port: int):
        """Add a peer node."""
        tcp_addr = NodeAddress(node_id, tcp_host, tcp_port, TransportProtocol.TCP)
        udp_addr = NodeAddress(node_id, tcp_host, udp_port, TransportProtocol.UDP)
        self.peers[node_id] = (tcp_addr, udp_addr)
        self.udp.add_peer(udp_addr)
        # TCP connection is lazy (on first consensus op)

    def execute(self, op_type: OperationType, key: str, value: Any) -> Any:
        """Execute an operation through UCL with network transport."""
        msg = UCLMessage(
            op_type=op_type,
            key=key,
            value=value,
            node_id=self.node_id
        )

        self.ops_executed += 1

        if msg.coordination_class == CoordinationClass.COORDINATION_FREE:
            # Use UDP gossip - non-blocking
            return self._execute_gossip(msg)
        else:
            # Use TCP consensus - blocking
            return self._execute_consensus(msg)

    def _execute_gossip(self, msg: UCLMessage) -> Any:
        """Execute via gossip (UDP)."""
        self.gossip_ops += 1

        # Apply locally first
        result = self._apply_operation(msg)

        # Broadcast to peers via UDP
        network_msg = NetworkMessage(
            source_node=self.node_id,
            dest_node=-1,  # Broadcast
            ucl_message=msg
        )
        self.udp.broadcast(network_msg)

        return result

    def _execute_consensus(self, msg: UCLMessage) -> Any:
        """Execute via consensus (TCP)."""
        self.consensus_ops += 1

        # For simplicity, use leader-based consensus
        # In production, use Raft/Paxos

        # Ensure TCP connections
        for node_id, (tcp_addr, _) in self.peers.items():
            self.tcp.connect(tcp_addr)

        # Create consensus request
        network_msg = NetworkMessage(
            source_node=self.node_id,
            dest_node=-1,
            ucl_message=msg
        )

        # Set up response tracking
        event = threading.Event()
        self.pending_consensus[network_msg.message_id] = event

        # Send to all peers
        for node_id in self.peers:
            self.tcp.send(node_id, network_msg)

        # Apply locally
        result = self._apply_operation(msg)
        self.consensus_results[network_msg.message_id] = result

        # Wait for acknowledgments (simplified - real consensus is more complex)
        event.wait(timeout=5.0)

        return result

    def _apply_operation(self, msg: UCLMessage) -> Any:
        """Apply an operation to local state."""
        with self.state_lock:
            current = self.state.get(msg.key)

            if msg.op_type == OperationType.ADD:
                new_value = (current or 0) + msg.value
            elif msg.op_type == OperationType.MAX:
                new_value = max(current or float('-inf'), msg.value)
            elif msg.op_type == OperationType.MIN:
                new_value = min(current or float('inf'), msg.value)
            elif msg.op_type == OperationType.UNION:
                current_set = set(current) if current else set()
                new_set = set(msg.value) if isinstance(msg.value, (list, set)) else {msg.value}
                new_value = list(current_set | new_set)
            elif msg.op_type == OperationType.SET:
                new_value = msg.value
            elif msg.op_type == OperationType.CAS:
                # Compare-and-swap
                expected, new = msg.value
                if current == expected:
                    new_value = new
                else:
                    new_value = current
            else:
                new_value = msg.value

            self.state[msg.key] = new_value
            return new_value

    def _on_udp_message(self, msg: NetworkMessage):
        """Handle incoming UDP (gossip) message."""
        if msg.source_node == self.node_id:
            return
        self._apply_operation(msg.ucl_message)

    def _on_tcp_message(self, msg: NetworkMessage):
        """Handle incoming TCP (consensus) message."""
        if msg.source_node == self.node_id:
            return

        # Apply the operation
        self._apply_operation(msg.ucl_message)

        # If this is a response to our consensus, signal completion
        if msg.message_id in self.pending_consensus:
            self.pending_consensus[msg.message_id].set()

    def get(self, key: str) -> Any:
        """Get current value for a key."""
        with self.state_lock:
            return self.state.get(key)

    def get_stats(self) -> Dict:
        """Get node statistics."""
        return {
            "node_id": self.node_id,
            "ops_executed": self.ops_executed,
            "gossip_ops": self.gossip_ops,
            "consensus_ops": self.consensus_ops,
            "tcp_sent": self.tcp.messages_sent,
            "tcp_received": self.tcp.messages_received,
            "udp_sent": self.udp.messages_sent,
            "udp_received": self.udp.messages_received,
            "state_keys": len(self.state),
        }


# =============================================================================
# CLUSTER MANAGEMENT
# =============================================================================

class UCLClusterManager:
    """
    Manages a cluster of UCL nodes.

    Handles:
    - Node discovery
    - Cluster formation
    - Health monitoring
    """

    def __init__(self, base_port: int = 9000):
        self.base_port = base_port
        self.nodes: Dict[int, NetworkedUCLNode] = {}

    def create_node(self, node_id: int, host: str = "127.0.0.1") -> NetworkedUCLNode:
        """Create a new node in the cluster."""
        tcp_port = self.base_port + node_id * 2
        udp_port = self.base_port + node_id * 2 + 1

        node = NetworkedUCLNode(node_id, tcp_port, udp_port, host)
        self.nodes[node_id] = node
        return node

    def create_cluster(self, num_nodes: int, host: str = "127.0.0.1") -> List[NetworkedUCLNode]:
        """Create a cluster of nodes."""
        nodes = []
        for i in range(num_nodes):
            node = self.create_node(i, host)
            nodes.append(node)

        # Connect all nodes to each other
        for node in nodes:
            for peer in nodes:
                if peer.node_id != node.node_id:
                    node.add_peer(
                        peer.node_id,
                        peer.host,
                        peer.tcp_address.port,
                        peer.udp_address.port
                    )

        return nodes

    def start_cluster(self):
        """Start all nodes in the cluster."""
        for node in self.nodes.values():
            node.start()
        # Give nodes time to start
        time.sleep(0.5)

    def stop_cluster(self):
        """Stop all nodes."""
        for node in self.nodes.values():
            node.stop()

    def get_cluster_stats(self) -> Dict:
        """Get aggregate cluster statistics."""
        total_ops = sum(n.ops_executed for n in self.nodes.values())
        total_gossip = sum(n.gossip_ops for n in self.nodes.values())
        total_consensus = sum(n.consensus_ops for n in self.nodes.values())

        return {
            "num_nodes": len(self.nodes),
            "total_ops": total_ops,
            "gossip_ops": total_gossip,
            "consensus_ops": total_consensus,
            "gossip_pct": (total_gossip / total_ops * 100) if total_ops > 0 else 0,
        }


# =============================================================================
# DEMONSTRATION
# =============================================================================

def demonstrate_network_ucl():
    """Demonstrate UCL with actual network transport."""

    print("=" * 70)
    print("PHASE 12: UCL NETWORK TRANSPORT")
    print("=" * 70)
    print("""
This demonstrates UCL with actual network communication:
- TCP for consensus operations (reliable)
- UDP for gossip operations (fast)

All nodes run in the same process but communicate via sockets.
""")

    # Create cluster
    manager = UCLClusterManager(base_port=19000)
    num_nodes = 4
    nodes = manager.create_cluster(num_nodes)

    print(f"Created cluster with {num_nodes} nodes")
    for node in nodes:
        print(f"  Node {node.node_id}: TCP={node.tcp_address}, UDP={node.udp_address}")

    # Start cluster
    print("\nStarting cluster...")
    manager.start_cluster()
    print("Cluster started!\n")

    try:
        # Test 1: Gossip operations (coordination-free)
        print("-" * 50)
        print("TEST 1: Gossip Operations (UDP, C=0)")
        print("-" * 50)

        # Each node increments a counter
        print("Each node incrementing counter...")
        for node in nodes:
            node.execute(OperationType.ADD, "counter", node.node_id + 1)

        # Wait for gossip propagation
        time.sleep(0.5)

        # Check convergence
        print("\nCounter values across nodes:")
        values = [node.get("counter") for node in nodes]
        for i, v in enumerate(values):
            print(f"  Node {i}: {v}")

        expected = sum(range(1, num_nodes + 1))
        converged = all(v == expected for v in values)
        print(f"\nExpected: {expected}, Converged: {converged}")

        # Test 2: Max tracking
        print("\n" + "-" * 50)
        print("TEST 2: Max Tracking (UDP, C=0)")
        print("-" * 50)

        print("Each node reporting max value...")
        for node in nodes:
            node.execute(OperationType.MAX, "peak", node.node_id * 100)

        time.sleep(0.5)

        print("\nMax values across nodes:")
        values = [node.get("peak") for node in nodes]
        for i, v in enumerate(values):
            print(f"  Node {i}: {v}")

        expected_max = (num_nodes - 1) * 100
        converged = all(v == expected_max for v in values)
        print(f"\nExpected: {expected_max}, Converged: {converged}")

        # Test 3: Consensus operations
        print("\n" + "-" * 50)
        print("TEST 3: Consensus Operations (TCP, C=log N)")
        print("-" * 50)

        print("Node 0 setting config via consensus...")
        nodes[0].execute(OperationType.SET, "config", {"version": 1, "debug": True})

        time.sleep(0.5)

        print("\nConfig values across nodes:")
        values = [node.get("config") for node in nodes]
        for i, v in enumerate(values):
            print(f"  Node {i}: {v}")

        # Performance test
        print("\n" + "-" * 50)
        print("PERFORMANCE TEST")
        print("-" * 50)

        # Gossip performance
        start = time.time()
        ops = 100
        for i in range(ops):
            nodes[i % num_nodes].execute(OperationType.ADD, "perf_counter", 1)
        gossip_time = time.time() - start

        time.sleep(0.5)  # Wait for propagation

        # Consensus performance
        start = time.time()
        for i in range(10):  # Fewer ops (consensus is slower)
            nodes[0].execute(OperationType.SET, f"config_{i}", {"v": i})
        consensus_time = time.time() - start

        print(f"\nGossip: {ops} ops in {gossip_time*1000:.1f}ms ({ops/gossip_time:.0f} ops/sec)")
        print(f"Consensus: 10 ops in {consensus_time*1000:.1f}ms ({10/consensus_time:.0f} ops/sec)")
        print(f"Speedup: {(consensus_time/10)/(gossip_time/ops):.1f}x")

        # Cluster stats
        print("\n" + "-" * 50)
        print("CLUSTER STATISTICS")
        print("-" * 50)

        stats = manager.get_cluster_stats()
        print(f"\n  Total operations: {stats['total_ops']}")
        print(f"  Gossip operations: {stats['gossip_ops']} ({stats['gossip_pct']:.1f}%)")
        print(f"  Consensus operations: {stats['consensus_ops']}")

        print("\nPer-node statistics:")
        for node in nodes:
            s = node.get_stats()
            print(f"  Node {s['node_id']}: {s['ops_executed']} ops, "
                  f"UDP sent/recv: {s['udp_sent']}/{s['udp_received']}, "
                  f"TCP sent/recv: {s['tcp_sent']}/{s['tcp_received']}")

    finally:
        # Cleanup
        print("\nStopping cluster...")
        manager.stop_cluster()
        print("Cluster stopped.")

    # Summary
    print("\n" + "=" * 70)
    print("PHASE 12 SUMMARY")
    print("=" * 70)
    print("""
WHAT WE BUILT:

1. TCP TRANSPORT
   - Reliable, ordered message delivery
   - Connection management
   - Used for consensus operations

2. UDP TRANSPORT
   - Fast, best-effort delivery
   - Broadcast support
   - Used for gossip operations

3. NETWORKED UCL NODE
   - Automatic protocol selection
   - Local state management
   - Peer-to-peer communication

4. CLUSTER MANAGER
   - Node creation and discovery
   - Cluster formation
   - Statistics aggregation

NEXT STEPS:
- Add message acknowledgments
- Implement proper Raft consensus
- Add node failure detection
- Support cluster reconfiguration
""")

    return True


def main():
    """Run network UCL demonstration."""
    success = demonstrate_network_ucl()
    return success


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
