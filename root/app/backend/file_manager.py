import os
import shutil
import uuid
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
    # Normalize and ensure relative path cannot escape SHARED_DIR (component-wise, not string prefix)
    clean_rel = Path((relative_path or "").lstrip("/\\"))
    target = (SHARED_DIR / clean_rel).resolve()
    if target != SHARED_DIR and not target.is_relative_to(SHARED_DIR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access outside shared directory is forbidden"
        )
    return target

def safe_filename(filename: str) -> str:
    # Browsers may send full client paths; keep only the final component
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", "..") or "\x00" in name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return name

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
    if not target.exists():
        raise HTTPException(status_code=404, detail="Folder not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Not a folder")

    items = []
    for entry in target.iterdir():
        try:
            stat = entry.stat()
            is_dir = entry.is_dir()
            rel_entry = entry.relative_to(SHARED_DIR).as_posix()
            items.append({
                "name": entry.name,
                "path": rel_entry,
                "is_dir": is_dir,
                "size_bytes": stat.st_size if not is_dir else 0,
                "size_formatted": format_size(stat.st_size) if not is_dir else "-",
                "modified_at": stat.st_mtime
            })
        except Exception:
            continue

    # Sort directories first, then alphabetically
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return items

async def save_uploaded_file(upload_file: UploadFile, relative_path: str = "", overwrite: bool = False) -> Dict[str, Any]:
    target_dir = safe_path(relative_path)
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="Upload destination is not a folder")

    filename = safe_filename(upload_file.filename)
    target_file = target_dir / filename
    if not target_file.resolve().is_relative_to(SHARED_DIR):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if target_file.is_dir():
        raise HTTPException(status_code=409, detail=f"A folder named {filename} already exists")
    if target_file.exists() and not overwrite:
        raise HTTPException(status_code=409, detail=f"{filename} already exists")

    # Write to a temp file first so a failed upload never leaves a truncated file behind
    tmp_file = target_dir / f".{filename}.{uuid.uuid4().hex}.part"
    try:
        async with aiofiles.open(tmp_file, "wb") as f:
            while chunk := await upload_file.read(1024 * 1024): # 1MB chunks
                await f.write(chunk)
        os.replace(tmp_file, target_file)
    except Exception:
        tmp_file.unlink(missing_ok=True)
        raise

    stat = target_file.stat()
    return {
        "success": True,
        "filename": filename,
        "path": target_file.relative_to(SHARED_DIR).as_posix(),
        "size_formatted": format_size(stat.st_size)
    }

def create_folder(relative_path: str, name: str) -> Dict[str, Any]:
    parent = safe_path(relative_path)
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent is not a folder")
    folder = parent / safe_filename(name)
    if folder.exists():
        raise HTTPException(status_code=409, detail=f"{folder.name} already exists")
    folder.mkdir()
    return {"success": True, "path": folder.relative_to(SHARED_DIR).as_posix()}

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
