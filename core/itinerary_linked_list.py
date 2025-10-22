# core/itinerary_linked_list.py
from typing import List, Optional, Dict, Any
import uuid


class ItineraryNode:
    def __init__(self, day: int, slot_name: str, start_time: str, end_time: str, places: List[Dict[str, Any]]):
        self.node_id = str(uuid.uuid4())  # ✅ 唯一識別碼
        self.day = day
        self.slot_name = slot_name
        self.start_time = start_time
        self.end_time = end_time
        self.places = places  # list of place dicts
        self.next: Optional["ItineraryNode"] = None

    def to_dict(self) -> dict:
        """
        轉為可存進 MongoDB 的 dict
        """
        return {
            "node_id": self.node_id,
            "day": self.day,
            "slot": self.slot_name,
            "start": self.start_time,
            "end": self.end_time,
            "places": self.places,
            "next_id": self.next.node_id if self.next else None
        }


def linked_to_list(head: Optional[ItineraryNode]) -> List[dict]:
    """
    將 Linked List 轉為 list of dicts，方便儲存進 MongoDB 或回傳
    """
    result = []
    current = head
    while current:
        result.append(current.to_dict())
        current = current.next
    return result


def build_linked_list(slots: List[dict]) -> Optional[ItineraryNode]:
    """
    根據 slot 資料建立 linked list
    slot 格式：{ "day": int, "slot": str, "start": str, "end": str, "places": [...] }
    """
    head = None
    prev = None

    for s in slots:
        node = ItineraryNode(
            day=s["day"],
            slot_name=s["slot"],
            start_time=s["start"],
            end_time=s["end"],
            places=s["places"]
        )
        if not head:
            head = node
        else:
            prev.next = node
        prev = node

    return head


def flatten_linked(head: Optional[ItineraryNode]) -> dict:
    """
    把 linked list 攤平成 { "head_id": <str>, "nodes": [ {...}, {...} ] }
    """
    nodes = []
    current = head
    while current:
        nodes.append(current.to_dict())
        current = current.next
    return {
        "head_id": head.node_id if head else None,
        "nodes": nodes
    }


def rebuild_linked(nodes: List[dict], head_id: Optional[str]) -> Optional[ItineraryNode]:
    """
    從 MongoDB 撈回的 nodes[] 與 head_id 還原 linked list
    """
    if not nodes or not head_id:
        return None

    # 建立 node_id -> Node 物件的映射
    id_map: Dict[str, ItineraryNode] = {}
    for n in nodes:
        node = ItineraryNode(
            day=n.get("day", 0),
            slot_name=n.get("slot", ""),
            start_time=n.get("start", ""),
            end_time=n.get("end", ""),
            places=n.get("places", []),
        )
        node.node_id = n.get("node_id", str(uuid.uuid4()))  # 保持與 DB 一致
        id_map[node.node_id] = node

    # 串接 next
    for n in nodes:
        nid = n.get("node_id")
        next_id = n.get("next_id")
        if nid in id_map and next_id and next_id in id_map:
            id_map[nid].next = id_map[next_id]

    return id_map.get(head_id)
