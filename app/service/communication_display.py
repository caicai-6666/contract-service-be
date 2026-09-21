"""用户展示投影：新任务读取真实精简事件，旧任务只兼容已有消息。"""

from copy import deepcopy

from app.schema.communication import ConversationDisplayPayload, TERMINAL_STATUSES


def display_payload(record) -> ConversationDisplayPayload:
    payload = record.payload
    recorded = "events" in payload
    events = deepcopy(payload.get("events", []))
    # 内部轨迹可能因工具穿插拆开同一条消息；只聚合正文，不公开工具信息。
    messages = {}
    for item in payload.get("trace", []):
        if item.get("type") != "message":
            continue
        message_id = item["message_id"]
        if message_id not in messages:
            messages[message_id] = {key: deepcopy(item[key]) for key in
                                    ("message_id", "message_kind", "text", "status", "references")}
        else:
            messages[message_id]["text"] += item["text"]
    streaming = []
    for message in messages.values():
        if message["status"] == "streaming" and record.status not in TERMINAL_STATUSES:
            streaming.append(message)
        elif not recorded:
            if message["status"] == "streaming":
                message["status"] = "interrupted"
            events.append({"sequence": None, "event": "message.completed",
                           "data": {"turn_id": record.turn_id, **message}})
    source = payload.get("input", {})
    if not isinstance(source, dict):
        source = {"text": source if isinstance(source, str) else None}
    return ConversationDisplayPayload(
        input={"text": source.get("text"), "files": deepcopy(source.get("files", [])),
               "contracts": deepcopy(source.get("contracts", []))},
        events=events, streaming_messages=streaming,
        last_sequence=payload.get("event_cursor", 0) if recorded else None,
        event_source="recorded" if recorded else "legacy",
    )
