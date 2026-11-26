"""命令行入口：处理语料并打印排程、交接与角色视图摘要。

    python3 -m reception
"""

import json

from materials import load_corpus
from .workflow import ReceptionProcessor


def main() -> None:
    processor = ReceptionProcessor()
    processor.ingest_corpus(load_corpus())

    print("== 行程版本 ==")
    for version in processor.coordinator_view()["itinerary_versions"]:
        print(f"v{version['version']}（{version['basis']}）"
              f" 可行={not version['violations']}")
        for segment in version["segments"]:
            print(f"  {segment['time']} [{segment['mode']}] "
                  f"{segment['language']} 字幕={segment['subtitle']}")
        for violation in version["violations"]:
            print(f"  违反约束：{violation}")

    print("\n== 成员安置 ==")
    for placement in processor.placements():
        print(f"{placement.member_id}: {placement.status.value}")
        for question in placement.open_questions:
            print(f"  待确认：{question}")
        for reason in placement.blocking_reasons:
            print(f"  阻断：{reason}")

    print("\n== 交接需求 ==")
    for handoff in processor.handoffs:
        print(f"[{handoff.status.value}] {handoff.topic} -> "
              f"{handoff.follow_up_owner}（{', '.join(handoff.member_ids)}）")

    print("\n== 发送状态 ==")
    print("dispatch_ready =", processor.dispatch_ready())

    split = processor.evaluate_split()
    print("\n== 临时拆组评估 ==")
    print("可行 =", split.is_feasible())
    for violation in split.violations:
        print(f"  {violation}")

    print("\n== 场馆视图（开埠街，不含个人原话） ==")
    print(json.dumps(processor.venue_view("开埠街"),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
