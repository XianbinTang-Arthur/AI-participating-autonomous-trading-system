"""Tiny helper: connect to NATS and print AATS_EVENTS stream info.

Used for Slice 6.5 real-run validation of ensure_stream idempotent upsert.
"""
from __future__ import annotations

import asyncio

import nats  # type: ignore[import-not-found]


async def main() -> None:
    nc = await nats.connect("nats://nats:4222")
    js = nc.jetstream()
    try:
        info = await js.stream_info("AATS_EVENTS")
        subjects = info.config.subjects or []
        has_obl = "aats.execution.obligation_updates" in subjects
        print(f"stream=AATS_EVENTS subject_count={len(subjects)}")
        print(f"has_obligation_updates={has_obl}")
        if has_obl:
            print("UP-TO-DATE: OBLIGATION_UPDATES already in stream")
        else:
            print("NEEDS-UPGRADE: OBLIGATION_UPDATES missing, next ensure_stream must update")
    except Exception as exc:
        print(f"stream_info failed: {type(exc).__name__}: {exc}")
    finally:
        await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
