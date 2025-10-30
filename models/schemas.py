from pydantic import BaseModel, Field, root_validator
from typing import List, Optional, Union, Dict, Any
# ✅ 團體成員只需要 user_id
class GroupMember(BaseModel):
    user_id: str

# ✅ 團體推薦請求格式（主揪填寫偏好、避開條件、備註）
class RecommendGroupRequest(BaseModel):
    locations: Union[str, List[str]] = Field(
        ..., description="城市；可為單一字串或字串陣列"
    )
    date: str
    # 可用字串 "09:00–20:00" 或物件 {"morning": True, "noon": True, ...}
    time_range: Union[str, dict] = Field(..., description="時間範圍，可為字串或物件")
    days: int
    leader_id: str                      # 主揪 ID
    members: List[GroupMember]          # 所有成員 user_id 清單

    # ✅ 用 default_factory 避免可變預設值陷阱
    preferences: List[str] = Field(default_factory=list)   # 主揪勾選的偏好
    exclude: List[str] = Field(default_factory=list)       # 主揪想避開的類型（可空）
    notes: Optional[str] = ""       # 與後端現有邏輯相容
    # ✅ 新增：行程名稱（個人與團體都會存進 DB）
    trip_name: Optional[str] = None
    
    @root_validator(pre=True)
    def _compat_location_to_locations(cls, values):
        # 允許舊前端傳 'location'，自動搬到 'locations'
        if "locations" not in values and "location" in values:
            values["locations"] = values.pop("location")
        return values


# ✅ 個人推薦：維持 form 是 dict，routes 會自行正規化到 locations[]
class RecommendRequest(BaseModel):
    user_id: str
    form: Dict[str, Any]

from pydantic import BaseModel, Field
from typing import Optional

# 【 新增 Alternative Pydantic 模型】
# 這個模型必須對應 Kotlin 的 Alternative data class
# 我們使用 Field(alias=...) 來接收 Kotlin 傳來的 camelCase (例如 "placeId")
# 並將它轉換為 Python 的 snake_case (例如 "place_id")
class Alternative(BaseModel):
    place_id: str = Field(..., alias="placeId")
    name: str
    address: Optional[str] = None
    rating: Optional[float] = None
    user_ratings_total: Optional[int] = Field(default=None, alias="userRatingsTotal")
    lat: float
    lng: float
    open_status_text: Optional[str] = Field(default=None, alias="openStatusText")
    photo_url: Optional[str] = Field(default=None, alias="photoUrl")

    class Config:
        populate_by_name = True # 允許 Pydantic 透過 alias="placeId" 來填充 place_id
        

# 【2. 新增 ReplaceActivityRequest Pydantic 模型】
# 這個模型必須對應 Kotlin 的 ReplaceActivityRequest data class
# 剛好 Kotlin 傳來的 JSON key (old_activity_id, new_activity_data) 
# 已經是 snake_case，所以我們可以直接定義
class ReplaceActivityRequest(BaseModel):
    old_activity_id: str
    new_activity_data: Alternative
