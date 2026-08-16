from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from enum import Enum


class BatchUploadStatusEnum(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    partially_completed = "partially_completed"
    failed = "failed"
    cancelled = "cancelled"


class BatchUploadItemStatusEnum(str, Enum):
    pending = "pending"
    uploading = "uploading"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class BatchUploadItemResponse(BaseModel):
    id: int
    filename: str
    status: BatchUploadItemStatusEnum
    error_message: Optional[str] = None
    acrcloud_file_id: Optional[str] = None

    class Config:
        from_attributes = True


class BatchUploadResponse(BaseModel):
    batch_id: int
    status: BatchUploadStatusEnum
    total_files: int
    completed_files: int
    failed_files: int
    cancelled_files: int
    items: List[BatchUploadItemResponse]
    created_at: datetime

    class Config:
        from_attributes = True


class BatchUploadCreateResponse(BaseModel):
    batch_id: int
    message: str
    total_files: int


class BatchCancelResponse(BaseModel):
    batch_id: int
    message: str
    cancelled_count: int
