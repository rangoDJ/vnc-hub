import os
import shutil
from pathlib import Path
from typing import List, Dict, Any
from fastapi import UploadFile, HTTPException, status
from fastapi.responses import FileResponse
import aiofiles

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared")).resolve()

def ensure_shared_dir():
    SHARED_DIR.mkdir(parents=True, exist_ok=True)

def safe_path(relative_path: str) -> Path:
    ensure_shared_dir()
    # Normalize and ensure relative path cannot escape SHARED_DIR
    clean_rel = Path(relative_path.lstrip("/\\"))
    target = (SHARED_DIR / clean_rel).resolve()
    if not str(target).startswith(str(SHARED_DIR)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access outside shared directory is forbidden"
        )
    return target

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

def list_files(relative_path: str = "") -> List[Dict[str, Any]]:
    target = safe_path(relative_path)
    if not target.exists() or not target.is_dir():
        return []

    items = []
    for entry in target.iterdir():
        try:
            stat = entry.stat()
            rel_entry = entry.relative_to(SHARED_DIR).as_posix()
            items.append({
                "name": entry.name,
                "path": rel_entry,
                "is_dir": entry.is_dir(),
                "size_bytes": stat.st_size if not entry.is_dir() else 0,
                "size_formatted": format_size(stat.st_size) if not entry.is_dir() else "-",
                "modified_at": stat.st_mtime
            })
        except Exception:
            continue

    # Sort directories first, then alphabetically
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return items

async def save_uploaded_file(upload_file: UploadFile, relative_path: str = "") -> Dict[str, Any]:
    target_dir = safe_path(relative_path)
    if not target_dir.is_dir():
        target_dir = target_dir.parent

    target_file = target_dir / upload_file.filename
    # Path traversal check on the combined file
    if not str(target_file.resolve()).startswith(str(SHARED_DIR)):
        raise HTTPException(status_code=400, detail="Invalid filename")

    async with aiofiles.open(target_file, "wb") as f:
        while chunk := await upload_file.read(1024 * 1024): # 1MB chunks
            await f.write(chunk)

    stat = target_file.stat()
    return {
        "success": True,
        "filename": upload_file.filename,
        "path": target_file.relative_to(SHARED_DIR).as_posix(),
        "size_formatted": format_size(stat.st_size)
    }

def get_file_for_download(relative_path: str) -> FileResponse:
    target = safe_path(relative_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream"
    )

def delete_path(relative_path: str) -> Dict[str, Any]:
    target = safe_path(relative_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File or folder not found")
    if target == SHARED_DIR:
        raise HTTPException(status_code=400, detail="Cannot delete root shared directory")

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"success": True, "message": f"Deleted {target.name}"}
