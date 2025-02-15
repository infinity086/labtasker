from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from labtasker.constants import Priority


class BaseApiModel(BaseModel):
    """
    Base API model for all API models.
    """

    model_config = ConfigDict(populate_by_name=True)


class HealthCheckResponse(BaseApiModel):
    status: str = Field(..., pattern=r"^(healthy|unhealthy)$")
    database: str


class QueueCreateRequest(BaseApiModel):
    queue_name: str = Field(
        ..., pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=100
    )
    password: SecretStr = Field(..., min_length=1, max_length=100)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)

    def to_request_dict(self):
        """
        Used to form a quest, since password must be revealed
        """
        result = self.model_dump()
        result.update({"password": self.password.get_secret_value()})
        return result


class QueueCreateResponse(BaseApiModel):
    queue_id: str


class QueueGetResponse(BaseApiModel):
    queue_id: str = Field(alias="_id")
    queue_name: str
    created_at: datetime
    last_modified: datetime
    metadata: Dict[str, Any]


class TaskSubmitRequest(BaseApiModel):
    """Task submission request."""

    task_name: Optional[str] = None
    args: Optional[Dict[str, Any]] = Field(default_factory=dict)
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    cmd: Optional[Union[str, List[str]]] = None
    heartbeat_timeout: Optional[int] = None
    task_timeout: Optional[int] = None
    max_retries: int = 3
    priority: int = Priority.MEDIUM


class TaskFetchRequest(BaseApiModel):
    worker_id: Optional[str] = None
    eta_max: Optional[str] = None
    heartbeat_timeout: Optional[int] = None
    start_heartbeat: bool = True
    required_fields: Optional[Dict[str, Any]] = None
    extra_filter: Optional[Dict[str, Any]] = None


# class TaskFetchTask(BaseApiModel):
#     task_id: str = Field(alias="_id")
#     args: Dict[str, Any]
#     metadata: Dict[str, Any]
#     created_at: datetime
#     heartbeat_timeout: Optional[int] = None
#     task_timeout: Optional[int] = None


class Task(BaseApiModel):
    task_id: str = Field(alias="_id")  # Accepts "_id" as an input field
    queue_id: str
    status: str
    task_name: Optional[str]
    created_at: datetime
    start_time: Optional[datetime]
    last_heartbeat: Optional[datetime]
    last_modified: datetime
    heartbeat_timeout: Optional[int]
    task_timeout: Optional[int]
    max_retries: int
    retries: int
    priority: int
    metadata: Dict
    args: Dict
    cmd: str
    summary: Dict
    worker_id: Optional[str]


class TaskFetchResponse(BaseApiModel):
    found: bool = False
    task: Optional[Task] = None


class TaskLsRequest(BaseApiModel):
    offset: int = 0
    limit: int = 100
    task_id: Optional[str] = None
    task_name: Optional[str] = None
    extra_filter: Optional[Dict[str, Any]] = None


class TaskLsResponse(BaseApiModel):
    found: bool = False
    content: List[Task] = Field(default_factory=list)


class TaskSubmitResponse(BaseApiModel):
    task_id: str


class TaskStatusUpdateRequest(BaseApiModel):
    status: str = Field(..., pattern=r"^(success|failed|cancelled)$")
    summary: Optional[Dict[str, Any]] = Field(default_factory=dict)


class WorkerCreateRequest(BaseApiModel):
    worker_name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    max_retries: Optional[int] = 3


class WorkerCreateResponse(BaseApiModel):
    worker_id: str


class WorkerStatusUpdateRequest(BaseApiModel):
    status: str = Field(..., pattern=r"^(active|suspended|failed)$")


class WorkerLsRequest(BaseApiModel):
    offset: int = 0
    limit: int = 100
    worker_id: Optional[str] = None
    worker_name: Optional[str] = None
    extra_filter: Optional[Dict[str, Any]] = None


class Worker(BaseApiModel):
    worker_id: str = Field(alias="_id")
    queue_id: str
    status: str
    worker_name: Optional[str]
    metadata: Dict
    retries: int
    max_retries: int
    created_at: datetime
    last_modified: datetime


class WorkerLsResponse(BaseApiModel):
    found: bool = False
    content: List[Worker] = Field(default_factory=list)


class QueueUpdateRequest(BaseApiModel):
    new_queue_name: Optional[str] = Field(
        None, pattern=r"^[a-zA-Z0-9_-]+$", min_length=1, max_length=100
    )
    new_password: Optional[SecretStr] = Field(None, min_length=1, max_length=100)
    metadata_update: Optional[Dict[str, Any]] = Field(default_factory=dict)

    def to_request_dict(self):
        """
        Used to form a quest, since password must be revealed
        """
        result = self.model_dump()
        if self.new_password:
            result.update({"new_password": self.new_password.get_secret_value()})
        return result
