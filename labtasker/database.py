from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from fastapi import HTTPException
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from .fsm import TaskFSM, TaskState, WorkerState
from .utils import flatten_dict, get_current_time, parse_timeout

if TYPE_CHECKING:
    from .security import SecurityManager


class Priority(int, Enum):
    LOW = 0
    MEDIUM = 10  # default
    HIGH = 20


class DatabaseClient:
    def __init__(
        self, uri: str = None, db_name: str = None, client: Optional[MongoClient] = None
    ):
        """Initialize database client."""
        from .security import SecurityManager  # Import here to avoid circular import

        self.security = SecurityManager()
        if client:
            # Use provided client (for testing)
            self.client = client
            self.db = self.client[db_name]
            self._setup_collections()
            return

        if not uri or not db_name:
            raise HTTPException(
                status_code=422,
                detail="Either provide uri and db_name or a client instance",
            )

        try:
            self.client = MongoClient(uri)
            if not isinstance(self.client, MongoClient):
                # Test connection only for real MongoDB (not mock)
                self.client.admin.command("ping")
            self.db: Database = self.client[db_name]
            self._setup_collections()
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to connect to MongoDB: {str(e)}"
            )

    def _setup_collections(self):
        """Setup collections and indexes."""
        # Queues collection
        self.queues: Collection = self.db.queues
        # _id is automatically indexed by MongoDB
        self.queues.create_index([("queue_name", ASCENDING)], unique=True)

        # Tasks collection
        self.tasks: Collection = self.db.tasks
        # _id is automatically indexed by MongoDB
        self.tasks.create_index([("queue_id", ASCENDING)])  # Reference to queue._id
        self.tasks.create_index([("status", ASCENDING)])
        self.tasks.create_index([("priority", DESCENDING)])  # Higher priority first
        self.tasks.create_index([("created_at", ASCENDING)])  # Older tasks first

        # Workers collection
        self.workers: Collection = self.db.workers
        # _id is automatically indexed by MongoDB
        self.workers.create_index([("queue_id", ASCENDING)])  # Reference to queue._id
        self.workers.create_index(
            [("worker_name", ASCENDING)]
        )  # Optional index for searching

    def query(
        self,
        queue_name: str,
        query: Dict[str, Any],  # MongoDB query
    ) -> bool:
        """
        Query a collection.
        Note: This function is too versatile and should be used with caution.
        """
        # Verify queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Make sure no trespassing
        if query["queue_id"] != queue["_id"]:
            raise HTTPException(
                status_code=400,
                detail="Query queue_id does not match the matching queue_id given by queue_name",
            )
        result = self.tasks.find(query)
        return list(result)

    def update(
        self,
        queue_name: str,
        query: Dict[str, Any],  # MongoDB query
        update: Dict[str, Any],  # MongoDB update
    ) -> bool:
        """
        Update a collection in general.
        Note: This function is too versatile and should be used with caution.
        """
        # Verify queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Update collection
        if query["queue_id"] != queue["_id"]:
            raise HTTPException(
                status_code=400,
                detail="Query queue_id does not match the matching queue_id given by queue_name",
            )

        now = get_current_time()

        if update.get("$set"):
            update["$set"]["last_modified"] = now
        else:
            update["$set"] = {"last_modified": now}

        result = self.tasks.update_many(query, update)
        return result.modified_count > 0

    def create_queue(
        self, queue_name: str, password: str, metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Create a new queue."""
        # Validate queue name
        if not queue_name or not isinstance(queue_name, str):
            raise HTTPException(status_code=400, detail="Invalid queue name")

        try:
            now = get_current_time()
            queue = {
                "_id": str(uuid4()),
                "queue_name": queue_name,
                "password": self.security.hash_password(password),
                "created_at": now,
                "last_modified": now,
                "metadata": metadata or {},
            }
            result = self.queues.insert_one(queue)
            return str(result.inserted_id)
        except DuplicateKeyError:
            raise HTTPException(
                status_code=HTTP_409_CONFLICT,
                detail=f"Queue '{queue_name}' already exists",
            )
        except ValueError as e:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except Exception as e:
            raise HTTPException(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create queue: {str(e)}",
            )

    def create_task(
        self,
        queue_name: str,
        task_name: Optional[str] = None,
        args: Dict[str, Any] = None,
        metadata: Dict[str, Any] = None,
        heartbeat_timeout: int = 60,
        task_timeout: Optional[
            int
        ] = None,  # Maximum time in seconds for task execution
        max_retries: int = 3,  # Maximum number of retries
        priority: int = Priority.MEDIUM,
    ) -> str:
        """Create a task related to a queue."""
        # Verify queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Validate args
        if args is not None and not isinstance(args, dict):
            raise HTTPException(
                status_code=400, detail="Task args must be a dictionary"
            )

        now = get_current_time()

        # fsm = TaskFSM(
        #     current_state=TaskState.PENDING, retries=0, max_retries=max_retries
        # )
        # fsm.reset()

        task = {
            "_id": str(uuid4()),
            "queue_id": str(queue["_id"]),
            "status": TaskState.PENDING,
            "task_name": task_name,
            "created_at": now,
            "start_time": None,
            "last_heartbeat": None,
            "last_modified": now,
            "heartbeat_timeout": heartbeat_timeout,
            "task_timeout": task_timeout,
            "max_retries": max_retries,
            "retries": 0,
            "priority": priority,
            "metadata": metadata or {},
            "args": args or {},
            "summary": {},
            "worker_id": None,
        }
        result = self.tasks.insert_one(task)
        return str(result.inserted_id)

    def create_worker(
        self,
        queue_name: str,
        worker_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
    ) -> str:
        """Create a worker."""
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        now = get_current_time()

        worker = {
            "_id": str(uuid4()),
            "queue_id": str(queue["_id"]),
            "status": WorkerState.ACTIVE,
            "worker_name": worker_name,
            "metadata": metadata or {},
            "retries": 0,
            "max_retries": max_retries,
            "created_at": now,
            "last_modified": now,
        }
        result = self.workers.insert_one(worker)
        return str(result.inserted_id)

    def delete_queue(
        self,
        queue_name: str,
        cascade_delete: bool = False,  # TODO: need consideration
    ) -> bool:
        """
        Delete a queue.

        Args:
            queue_name (str): The name of the queue to delete.
            cascade_delete (bool): Whether to delete all tasks and workers in the queue.
        """
        # Make sure queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Delete queue
        self.queues.delete_one({"_id": queue["_id"]})

        if cascade_delete:
            # Delete all tasks in the queue
            self.tasks.delete_many({"queue_id": queue["_id"]})
            # Delete all workers in the queue
            self.workers.delete_many({"queue_id": queue["_id"]})

        return True

    def delete_task(
        self,
        queue_name: str,
        task_id: str,
    ) -> bool:
        """Delete a task."""
        # Verify queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Delete task
        result = self.tasks.delete_one({"_id": task_id, "queue_id": queue["_id"]})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Task not found")
        return result.deleted_count > 0

    def delete_worker(
        self, queue_name: str, worker_id: str, cascade_update: bool = True
    ) -> bool:
        """
        Delete a worker.

        Args:
            queue_name (str): The name of the queue to delete the worker from.
            worker_id (str): The ID of the worker to delete.
            cascade_update (bool): Whether to set worker_id to None for associated tasks.
        """
        # Verify queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Delete worker
        worker_result = self.workers.delete_one(
            {"_id": worker_id, "queue_id": queue["_id"]}
        )
        if worker_result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Worker not found")

        now = get_current_time()
        if cascade_update:
            # Update all tasks associated with the worker
            self.tasks.update_many(
                {"queue_id": queue["_id"], "worker_id": worker_id},
                {"$set": {"worker_id": None, "last_modified": now}},
            )

        return True

    def update_queue(
        self,
        queue_name: str,
        new_queue_name: Optional[str] = None,
        new_password: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update queue settings."""
        # Verify queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        queue_name = new_queue_name or queue["queue_name"]
        password = (
            self.security.hash_password(new_password)
            if new_password
            else queue["password"]
        )
        metadata = (
            queue["metadata"].update(metadata_update)
            if metadata_update
            else queue["metadata"]
        )

        # Update queue settings
        update = {
            "$set": {
                "queue_name": queue_name,
                "password": password,
                "metadata": metadata,
                "last_modified": get_current_time(),
            }
        }
        result = self.queues.update_one({"_id": queue["_id"]}, update)
        return result.modified_count > 0

    def fetch_task(
        self,
        queue_name: str,
        worker_id: Optional[str] = None,
        eta_max: Optional[str] = None,
        extra_filter: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch next available task from queue.
        1. Fetch task from queue
        2. Set task status to RUNNING
        3. Set task worker_id to worker_id (if provided)
        4. Update related timestamps
        5. Return task

        Args:
            queue_name (str): The name of the queue to fetch the task from.
            worker_id (str, optional): The ID of the worker to assign the task to.
            eta_max (str, optional): The maximum time to wait for the task to be available.
            extra_filter (Dict[str, Any], optional): Additional filter criteria for the task.
        """
        task_timeout = parse_timeout(eta_max) if eta_max else None

        # Get queue ID
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Verify worker exists and is active
        if worker_id:
            worker_info = self.workers.find_one(
                {"_id": worker_id, "queue_id": queue["_id"]}
            )
            if not worker_info:
                raise HTTPException(
                    status_code=404,
                    detail=f"Worker '{worker_id}' not found in queue '{queue_name}'",
                )
            worker_status = worker_info["status"]
            if worker_status != WorkerState.ACTIVE:
                raise HTTPException(
                    status_code=400,
                    detail=f"Worker '{worker_id}' is {worker_status} in queue '{queue_name}'",
                )

        # Fetch task
        now = get_current_time()

        query = {
            "queue_id": queue["_id"],
            "status": TaskState.PENDING,
            **(extra_filter or {}),
        }

        update = {
            "$set": {
                "status": TaskState.RUNNING,
                "start_time": now,
                "last_heartbeat": now,
                "last_modified": now,
                "worker_id": worker_id,
            }
        }

        if task_timeout:
            update["$set"]["task_timeout"] = task_timeout

        # Find and update an available task
        # PENDING -> RUNNING
        result = self.tasks.find_one_and_update(
            query,
            update,
            sort=[("priority", -1), ("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return result

    def update_task_status(
        self,
        queue_name: str,
        task_id: str,
        report_status: str,
        summary_update: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Update task status. Used for reporting task execution results.
        """

        # Get queue ID
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        task = self.tasks.find_one({"_id": task_id, "queue_id": queue["_id"]})
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        try:
            fsm = TaskFSM.from_db_entry(task)

            if report_status == "success":
                fsm.complete()
            elif report_status == "failed":
                fsm.fail()
            elif report_status == "cancelled":
                fsm.cancel()
            else:
                raise HTTPException(
                    status_code=HTTP_400_BAD_REQUEST,
                    detail=f"Invalid report_status: {report_status}",
                )

        except Exception as e:
            raise HTTPException(
                status_code=HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

        summary_update = summary_update or {}
        summary_update = flatten_dict(summary_update, parent_key="summary")

        update = {
            "$set": {
                "status": fsm.state,
                "retries": fsm.retries,
                "last_modified": get_current_time(),
                **summary_update,
            }
        }

        result = self.tasks.update_one({"_id": task_id}, update)
        return result.modified_count > 0

    def update_task_and_reset_pending(
        self,
        queue_name: str,
        task_id: str,
        task_setting_update: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Update task settings (optional) and set task status to PENDING.
        Can be used to manually restart crashed tasks after max retries.

        Args:
            queue_name (str): The name of the queue to update the task in.
            task_id (str): The ID of the task to update.
            task_setting_update (Dict[str, Any], optional): A dictionary of task settings to update.
        """
        # Get queue ID
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        now = get_current_time()

        # Update task settings
        task_setting_update = task_setting_update or {}
        task_setting_update = flatten_dict(task_setting_update)

        task_setting_update["last_modified"] = now
        task_setting_update["status"] = TaskState.PENDING
        task_setting_update["retries"] = 0

        update = {
            "$set": {
                **task_setting_update,
            }
        }

        result = self.tasks.update_one(
            {"_id": task_id, "queue_id": queue["_id"]}, update
        )
        return result.modified_count > 0

    def cancel_task(
        self,
        queue_name: str,
        task_id: str,
    ) -> bool:
        """Cancel a task."""
        # Verify queue exists
        queue = self.queues.find_one({"queue_name": queue_name})
        if not queue:
            raise HTTPException(
                status_code=404, detail=f"Queue '{queue_name}' not found"
            )

        # Cancel task
        result = self.tasks.update_one(
            {"_id": task_id, "queue_id": queue["_id"]},
            {"$set": {"status": TaskState.CANCELLED}},
        )
        return result.modified_count > 0

    def handle_timeouts(self) -> List[str]:
        """Check and handle task timeouts."""
        now = get_current_time()
        transitioned_tasks = []

        # Build query
        query = {
            "status": TaskState.RUNNING,
            "$or": [
                # Heartbeat timeout
                {
                    "last_heartbeat": {"$ne": None},
                    "heartbeat_timeout": {"$ne": None},
                    "$expr": {
                        "$gt": [
                            {
                                "$divide": [
                                    {"$subtract": [now, "$last_heartbeat"]},
                                    1000,
                                ]
                            },
                            "$heartbeat_timeout",
                        ]
                    },
                },
                # Task execution timeout
                {
                    "task_timeout": {"$ne": None},
                    "start_time": {"$ne": None},
                    "$expr": {
                        "$gt": [
                            {"$divide": [{"$subtract": [now, "$start_time"]}, 1000]},
                            "$task_timeout",
                        ]
                    },
                },
            ],
        }

        # Find tasks that might have timed out
        tasks = self.tasks.find(query)

        tasks = list(tasks)  # Convert cursor to list

        for task in tasks:
            try:
                # Create FSM with current state
                fsm = TaskFSM(
                    current_state=task["status"],
                    retries=task.get("retries"),
                    max_retries=task.get("max_retries"),
                )

                # Transition to FAILED state through FSM
                fsm.fail()

                # Update task in database
                result = self.tasks.update_one(
                    {"_id": task["_id"]},
                    {
                        "$set": {
                            "status": fsm.state,
                            "retries": fsm.retries,
                            "last_modified": now,
                            "summary.labtasker_error": "Either heartbeat or task execution timed out",
                        }
                    },
                )
                if result.modified_count > 0:
                    transitioned_tasks.append(task["_id"])
            except Exception as e:
                # Log error but continue processing other tasks
                print(
                    f"Error handling timeout for task {task['_id']}: {e}"
                )  # TODO: log

        return transitioned_tasks
