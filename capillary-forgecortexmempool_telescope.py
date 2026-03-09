"""
Direct WebSocket connections to RPC endpoints for real-time mempool monitoring.
Implements redundant connections and heartbeat verification.
"""
import asyncio
import json
import logging
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from enum import Enum
import hashlib

import websockets
from web3 import Web3
from web3.types import TxData

logger = logging.getLogger(__name__)

class Chain(Enum):
    BASE = "base"
    POLYGON = "polygon"

@dataclass
class PendingTransaction:
    """Normalized pending transaction data"""
    hash: str
    from_address: str
    to_address: Optional[str]
    value: int
    gas_price: int
    gas: int
    input_data: str
    chain: Chain
    received_at: float
    first_seen_block: Optional[int]

class MempoolTelescope:
    """Real-time mempool monitoring with redundant WebSocket connections"""
    
    def __init__(self, chain: Chain, rpc_urls: List[str]):
        self.chain = chain
        self.rpc_urls = rpc_urls
        self.websocket_connections: Dict[str, websockets.WebSocketClientProtocol] = {}
        self.active_transactions: Dict[str, PendingTransaction] = {}
        self.transaction_hashes_seen: Set[str] = set()
        self.is_running = False
        self.last_block_number: Optional[int] = None
        
        # Statistics
        self.stats = {
            'tx_received': 0,
            'tx_dropped': 0,
            'connection_errors': 0,
            'last_heartbeat': None
        }
    
    async def _establish_connection(self, rpc_url: str) -> bool:
        """Establish WebSocket connection to RPC endpoint"""
        try:
            # Convert HTTPS URL to WebSocket
            if rpc_url.startswith('https://'):
                ws_url = rpc_url.replace('https://', 'wss://')
            else:
                ws_url = rpc_url
            
            logger.info(f"Connecting to {ws_url}")
            
            # Add authentication headers if needed
            headers = {}
            if 'alchemy.com' in ws_url:
                # Alchemy requires Origin header
                headers['Origin'] = 'https://dashboard.alchemy.com'
            
            ws = await websockets.connect(
                ws_url,
                extra_headers=headers,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5
            )
            
            # Subscribe to new pending transactions
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["newPendingTransactions"]
            }
            
            await ws.send(json.dumps(subscribe_msg))
            response = await ws.recv()
            response_data = json.loads(response)
            
            if 'result' in response_data:
                subscription_id = response_data['result']
                self.websocket_connections[rpc_url] = ws
                logger.info(f"Subscribed to pending transactions on {rpc_url}, subscription: {subscription_id}")
                return True
            else:
                logger.error(f"Failed to subscribe on {rpc_url}: {response_data}")
                return False
                
        except Exception as e:
            logger.error(f"Connection failed to {rpc_url}: {str(e)}")
            self.stats['connection_errors'] += 1
            return False
    
    async def _process_transaction(self, tx_hash: str, rpc_url: str) -> None:
        """Process a new pending transaction"""
        try:
            # Skip if recently seen (deduplication across RPCs)
            if tx_hash in self.transaction_hashes_seen:
                return
            
            self.transaction_hashes_seen.add(tx_hash)
            
            # Get transaction details from RPC
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            tx_data = w3.eth.get_transaction(tx_hash)
            
            if not tx_data:
                logger.warning(f"Could not get details for tx {tx_hash}")
                return
            
            # Create normalized transaction object
            pending_tx = PendingTransaction(
                hash=tx_hash,
                from_address=tx_data['from'],
                to_address=tx_data.get('to'),
                value=tx_data['value'],
                gas_price=tx_data['gasPrice'],
                gas=tx_data['gas'],
                input_data=tx_data['input'],
                chain=self.chain,
                received_at=asyncio.get_event_loop().time(),
                first_seen_block=self.last_block_number
            )
            
            # Store in active transactions
            self.active_transactions[tx_hash] = pending_tx
            self.stats['tx_received'] += 1
            
            # Analyze for arbitrage potential
            await self._analyze_for_arbitrage(pending_tx)
            
            # Clean old transactions periodically
            if len(self.active_transactions) > 10000:
                await self._clean_old_trans