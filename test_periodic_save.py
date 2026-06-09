#!/usr/bin/env python3
"""Test script to verify periodic save functionality."""

import os
import json
import threading
import time
from pathlib import Path

# Configuration
QUEUE_FILE = Path("/tmp/test-queue.json")
QUEUE_SAVE_INTERVAL = 5  # seconds

# Simulated downloads dict
downloads = {
    "test-1": {"id": "test-1", "status": "downloading"},
    "test-2": {"id": "test-2", "status": "queued"}
}

queue_save_timer = None
queue_save_lock = threading.Lock()

def save_queue():
    """Save download queue to file with atomic write."""
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Create snapshot
        queue_snapshot = json.loads(json.dumps(downloads, default=str))

        temp_file = QUEUE_FILE.with_suffix('.tmp')
        with open(temp_file, "w") as f:
            json.dump(queue_snapshot, f, indent=2, default=str)

        temp_file.replace(QUEUE_FILE)
        print(f"[SAVE] Saved queue with {len(queue_snapshot)} downloads to {QUEUE_FILE}")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to save queue: {e}")
        return False

def periodic_queue_save():
    """Periodically save the queue state."""
    global queue_save_timer
    print(f"[TIMER] Periodic queue save triggered at {time.time():.0f}")
    try:
        save_queue()
    except Exception as e:
        print(f"[ERROR] Failed in periodic save: {e}")

    with queue_save_lock:
        queue_save_timer = threading.Timer(QUEUE_SAVE_INTERVAL, periodic_queue_save)
        queue_save_timer.daemon = True
        queue_save_timer.start()
        print(f"[TIMER] Next save scheduled in {QUEUE_SAVE_INTERVAL} seconds")

def start_periodic_save():
    """Start the periodic save timer."""
    global queue_save_timer
    print("[INIT] Starting periodic save timer...")
    with queue_save_lock:
        if queue_save_timer is None or not queue_save_timer.is_alive():
            queue_save_timer = threading.Timer(QUEUE_SAVE_INTERVAL, periodic_queue_save)
            queue_save_timer.daemon = True
            queue_save_timer.start()
            print(f"[INIT] Timer started, first save in {QUEUE_SAVE_INTERVAL} seconds")
            print(f"[INIT] Timer thread: {queue_save_timer}")
            print(f"[INIT] Timer is alive: {queue_save_timer.is_alive()}")

def main():
    print("=" * 60)
    print("DRY RUN: Periodic Queue Save Test")
    print("=" * 60)

    # Clean up any existing file
    if QUEUE_FILE.exists():
        QUEUE_FILE.unlink()
        print("[CLEANUP] Removed existing queue file")

    # Start periodic save
    start_periodic_save()

    # Wait for first save
    print("\n[WAIT] Waiting for first periodic save...")
    time.sleep(QUEUE_SAVE_INTERVAL + 1)

    # Check if file was created
    if QUEUE_FILE.exists():
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        print(f"\n[RESULT] ✓ Queue file created with {len(data)} entries")
        print(f"[RESULT] File contents: {json.dumps(data, indent=2)}")
    else:
        print(f"\n[RESULT] ✗ Queue file NOT created!")

    # Wait for second save
    print(f"\n[WAIT] Waiting for second periodic save...")
    time.sleep(QUEUE_SAVE_INTERVAL + 1)

    # Modify downloads to test if changes are saved
    downloads["test-3"] = {"id": "test-3", "status": "queued"}
    print(f"\n[MODIFY] Added test-3 to downloads")

    # Wait for third save
    print(f"\n[WAIT] Waiting for third periodic save...")
    time.sleep(QUEUE_SAVE_INTERVAL + 1)

    if QUEUE_FILE.exists():
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        print(f"\n[RESULT] Queue file now has {len(data)} entries")
        if "test-3" in data:
            print(f"[RESULT] ✓ New entry test-3 was saved!")
        else:
            print(f"[RESULT] ✗ New entry test-3 was NOT saved!")
    else:
        print(f"\n[RESULT] ✗ Queue file NOT created!")

    print("\n" + "=" * 60)
    print("DRY RUN COMPLETE")
    print("=" * 60)

    # Clean up
    if QUEUE_FILE.exists():
        QUEUE_FILE.unlink()
        print("[CLEANUP] Removed test queue file")

if __name__ == "__main__":
    main()
