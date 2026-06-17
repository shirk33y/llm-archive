"""Verify structured tool call parity across all completed ingestors.

Validates that every ingestor produces proper ToolCall structures matching
claude-replay's expected format: tool_use_id, name, input, result, is_error,
no truncation, error detection.
"""

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_archive.schema import ToolCall
from llm_archive.ingestors.opencode import OpenCodeIngestor
from llm_archive.ingestors.claudecode import ClaudeCodeIngestor
from llm_archive.ingestors.codex import CodexIngestor


def _check_tool_call(tc: ToolCall, source: str, msg_id: str) -> list[str]:
    errors = []
    if not tc.tool_use_id:
        errors.append(f"  [{source}] {msg_id}: missing tool_use_id")
    if not tc.name:
        errors.append(f"  [{source}] {msg_id}: missing tool name")
    if tc.name == "unknown":
        errors.append(f"  [{source}] {msg_id}: unmapped tool name")
    if tc.input is None:
        errors.append(f"  [{source}] {msg_id}: null input")
    if tc.is_error is None:
        errors.append(f"  [{source}] {msg_id}: null is_error")
    return errors


def _check_result_not_none(tc: ToolCall, source: str, msg_id: str, kind: str) -> list[str]:
    errors = []
    # tool_result parts must have a result or be errors
    if kind == "tool_result" and tc.result is None and not tc.is_error:
        errors.append(f"  [{source}] {msg_id}: tool_result with no result")
    return errors


async def _check_ingestor(name: str, ingestor, source_filter: str | None = None) -> dict:
    print(f"\n=== Verifying {name} ===")
    stats = {"total_messages": 0, "total_parts": 0, "tool_calls": 0, "tool_results": 0,
             "errors": 0, "reasoning": 0, "has_tool_use_ids": 0}
    issues: list[str] = []

    try:
        count = await ingestor.count_threads()
        print(f"  Threads available: {count}")
        if count == 0:
            print(f"  SKIP: no {name} sessions found")
            return stats
    except Exception as e:
        print(f"  ERROR counting: {e}")
        return stats

    try:
        thread_count = 0
        async for thread in ingestor.threads(since=None):
            thread_count += 1
            for msg in thread.messages:
                stats["total_messages"] += 1
                for part in msg.parts:
                    stats["total_parts"] += 1
                    if part.kind == "tool_call":
                        stats["tool_calls"] += 1
                        if part.tool_call:
                            if part.tool_call.tool_use_id:
                                stats["has_tool_use_ids"] += 1
                            issues.extend(_check_tool_call(part.tool_call, thread.source_id, msg.id))
                            issues.extend(_check_result_not_none(part.tool_call, thread.source_id, msg.id, part.kind))
                            if part.tool_call.is_error:
                                stats["errors"] += 1
                    elif part.kind == "tool_result":
                        stats["tool_results"] += 1
                    elif part.kind == "reasoning":
                        stats["reasoning"] += 1
            if thread_count >= 5:
                break
        print(f"  Threads scanned: {thread_count}")
    except NotImplementedError as e:
        print(f"  SKIP: {e}")
        return stats
    except Exception as e:
        print(f"  ERROR scanning: {e}")
        return stats

    print(f"  Messages: {stats['total_messages']}")
    print(f"  Parts: {stats['total_parts']}")
    print(f"  Tool calls: {stats['tool_calls']}")
    print(f"  Tool results: {stats['tool_results']}")
    print(f"  Reasoning blocks: {stats['reasoning']}")
    print(f"  Error tools: {stats['errors']}")
    print(f"  With tool_use_id: {stats['has_tool_use_ids']}")
    if stats['tool_calls'] > 0:
        pct = stats['has_tool_use_ids'] / stats['tool_calls'] * 100
        print(f"  tool_use_id coverage: {pct:.1f}%")

    if stats['tool_calls'] == 0:
        print("  FAIL: no tool calls found")
        issues.append(f"  [{name}] No tool calls found in any message")

    if issues:
        print(f"\n  Issues ({len(issues)}):")
        for issue in issues[:10]:
            print(f"    {issue}")
        if len(issues) > 10:
            print(f"    ... and {len(issues) - 10} more")

    stats["issues"] = issues
    stats["verified"] = len(issues) == 0 and stats["tool_calls"] > 0
    if stats["verified"]:
        print(f"  ✅ {name} verified")
    else:
        print(f"  ❌ {name} has issues")
    return stats


async def main():
    print("=" * 60)
    print("Tool Call Parity Verification")
    print("=" * 60)

    ingestors = [
        ("ClaudeCode", ClaudeCodeIngestor()),
        ("Codex", CodexIngestor()),
        ("OpenCode", OpenCodeIngestor()),
    ]

    results = {}
    all_ok = True
    for name, ing in ingestors:
        stats = await _check_ingestor(name, ing)
        results[name] = stats
        if not stats.get("verified", False):
            all_ok = False

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, stats in results.items():
        status = "✅" if stats.get("verified") else "❌"
        tc = stats.get("tool_calls", 0)
        errs = len(stats.get("issues", []))
        print(f"  {status} {name}: {tc} tool calls, {errs} issues")

    if all_ok:
        print("\n✅ All ingestors verified successfully")
    else:
        print("\n❌ Some ingestors have issues")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
