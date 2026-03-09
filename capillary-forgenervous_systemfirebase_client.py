"""
CRITICAL: Primary state management system for Operation Capillary Forge.
All nodes synchronize through this Firebase interface - no local state persistence.
Implements optimistic locking for multi-node coordination.
"""
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
import logging

import firebase_admin
from firebase_admin import firestore, credentials, db
from google.cloud.firestore_v1 import Client as FirestoreClient
from google.cloud.firestore_v1.base_query import FieldFilter
from tenacity import retry, stop_after_attempt, wait_exponential

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class SystemState:
    """Immutable system state record for node coordination"""
    node_id: str
    timestamp: datetime
    last_block_processed: int
    status: str  # 'HEALTHY', 'DEGRADED', 'FAILED'
    cpu_usage: float
    memory_usage: float
    pending_opportunities: int
    last_heartbeat: datetime

class FirebaseStateManager:
    """Singleton Firebase manager for distributed state coordination"""
    
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._initialize_firebase()
            self._initialized = True
    
    def _initialize_firebase(self) -> None:
        """Initialize Firebase with proper error handling"""
        try:
            # CRITICAL: Service account key must exist
            cred_path = os.getenv('FIREBASE_SERVICE_ACCOUNT_KEY_PATH')
            if not cred_path or not os.path.exists(cred_path):
                raise FileNotFoundError(
                    f"Firebase service account key not found at {cred_path}. "
                    "Generate via Firebase Console > Project Settings > Service Accounts"
                )
            
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred, {
                'projectId': os.getenv('FIREBASE_PROJECT_ID'),
                'databaseURL': f"https://{os.getenv('FIREBASE_PROJECT_ID')}.firebaseio.com"
            })
            
            self.firestore_client: FirestoreClient = firestore.client()
            self.realtime_db = db.reference()
            
            logger.info("Firebase initialized successfully")
            
        except Exception as e:
            logger.error(f"Firebase initialization failed: {str(e)}")
            # Send immediate alert via Telegram if configured
            self._send_emergency_alert(f"Firebase init failed: {str(e)}")
            raise
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def update_system_state(self, state: SystemState) -> bool:
        """Update node state with optimistic locking"""
        try:
            node_ref = self.firestore_client.collection('system_state').document(state.node_id)
            
            # Read-modify-write with transaction for consistency
            transaction = self.firestore_client.transaction()
            
            @firestore.transactional
            def update_in_transaction(transaction, node_ref, new_state):
                snapshot = node_ref.get(transaction=transaction)
                current = snapshot.to_dict() if snapshot.exists else {}
                
                # Check for stale update (optimistic lock)
                if current and 'timestamp' in current:
                    current_time = current['timestamp']
                    if isinstance(current_time, datetime):
                        # Reject if existing state is newer (prevent rollback)
                        if current_time > new_state.timestamp:
                            logger.warning(f"Stale update rejected for {state.node_id}")
                            return False
                
                # Update with new state
                transaction.set(node_ref, asdict(new_state))
                return True
            
            success = update_in_transaction(transaction, node_ref, state)
            
            if success:
                logger.debug(f"Updated system state for {state.node_id}")
            return success
            
        except Exception as e:
            logger.error(f"Failed to update system state: {str(e)}")
            return False
    
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
    async def log_execution(self, tx_hash: str, data: Dict[str, Any]) -> bool:
        """Immutable execution logging - write once, never modify"""
        try:
            doc_ref = self.firestore_client.collection('execution_logs').document(tx_hash)
            
            # Ensure document doesn't already exist (immutability)
            if doc_ref.get().exists:
                logger.warning(f"Execution log already exists for {tx_hash}")
                return False
            
            # Add metadata
            data['logged_at'] = datetime.utcnow()
            data['immutable'] = True
            
            doc_ref.set(data)
            logger.info(f"Logged execution {tx_hash}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to log execution: {str(e)}")
            return False
    
    async def get_coordinator_state(self) -> Optional[Dict[str, Any]]:
        """Get the current coordinator state for block synchronization"""
        try:
            doc_ref = self.firestore_client.collection('system_state').document('coordinator')
            doc = doc_ref.get()
            
            if doc.exists:
                return doc.to_dict()
            return None
            
        except Exception as e:
            logger.error(f"Failed to get coordinator state: {str(e)}")
            return None
    
    async def queue_opportunity(self, opportunity: Dict[str, Any]) -> str:
        """Queue a validated opportunity for execution"""
        try:
            # Generate deterministic ID from opportunity data
            import hashlib
            opp_id = hashlib.sha256(
                f"{opportunity['chain']}_{opportunity['block_number']}_{opportunity['pair']}".encode()
            ).hexdigest()[:16]
            
            doc_ref = self.firestore_client.collection('opportunity_queue').document(opp_id)
            
            # Check if already queued
            if doc_ref.get().exists:
                logger.debug(f"Opportunity {opp_id} already queued")
                return opp_id
            
            # Add metadata and queue
            opportunity['queued_at'] = datetime.utcnow()
            opportunity['status'] = 'PENDING'
            opportunity['assigned_to'] = None
            
            doc_ref.set(opportunity)
            logger.info(f"Queued opportunity {opp_id}")
            return opp_id
            
        except Exception as e:
            logger.error(f"Failed to queue opportunity: {str(e)}")
            raise
    
    def _send_emergency_alert(self, message: str) -> None:
        """Send emergency alert via Telegram"""
        telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        if telegram_token and chat_id:
            try:
                import requests
                url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                payload = {
                    'chat_id': chat_id,
                    'text': f"🚨 CAPILLARY FORGE EMERGENCY: {message}",
                    'parse_mode': 'HTML'
                }
                requests.post(url, json=payload, timeout=5)
            except Exception as e:
                logger.error(f"Failed to send Telegram alert: {str(e)}")
    
    async def cleanup_stale_opportunities(self, max_age_hours: int = 1) -> int:
        """Clean up stale opportunities from queue"""
        try:
            cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
            
            query = self.firestore_client.collection('opportunity_queue').where(
                FieldFilter('queued_at', '<', cutoff)
            ).where(
                FieldFilter('status', 'in', ['PENDING', 'ASSIGNED'])
            )
            
            docs = query.stream()
            deleted = 0
            
            for doc in docs:
                doc.reference.delete()
                deleted += 1
            
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} stale opportunities")
            
            return deleted
            
        except Exception as e:
            logger.error(f"Failed to cleanup stale opportunities: {str(e)}")
            return 0

# Global singleton instance
firebase_manager = FirebaseStateManager()