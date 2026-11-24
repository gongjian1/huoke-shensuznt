"""IP 属地归一化：各平台返回的地区写法不一（中文省名、英文拼音、带"省/市"后缀、城市、国外），统一成省级中文名。"""
import re

PROVINCES = [
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建", "江西",
    "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海",
    "宁夏", "新疆", "内蒙古", "香港", "澳门", "台湾",
]
OVERSEAS = "海外"

GROUPS = [
    ("华东", ["上海", "江苏", "浙江", "安徽", "福建", "江西", "山东"]),
    ("华南", ["广东", "广西", "海南"]),
    ("华中", ["河南", "湖北", "湖南"]),
    ("华北", ["北京", "天津", "河北", "山西", "内蒙古"]),
    ("西南", ["重庆", "四川", "贵州", "云南", "西藏"]),
    ("西北", ["陕西", "甘肃", "青海", "宁夏", "新疆"]),
    ("东北", ["辽宁", "吉林", "黑龙江"]),
    ("港澳台", ["香港", "澳门", "台湾"]),
]

_EN = {
    "anhui": "安徽", "beijing": "北京", "chongqing": "重庆", "fujian": "福建", "gansu": "甘肃", "guangdong": "广东",
    "guangxi": "广西", "guizhou": "贵州", "hainan": "海南", "hebei": "河北", "heilongjiang": "黑龙江", "henan": "河南",
    "hubei": "湖北", "hunan": "湖南", "jiangsu": "江苏", "jiangxi": "江西", "jilin": "吉林", "liaoning": "辽宁",
    "inner mongolia": "内蒙古", "neimenggu": "内蒙古", "ningxia": "宁夏", "qinghai": "青海", "shaanxi": "陕西",
    "shandong": "山东", "shanghai": "上海", "shanxi": "山西", "sichuan": "四川", "tianjin": "天津", "tibet": "西藏",
    "xizang": "西藏", "xinjiang": "新疆", "yunnan": "云南", "zhejiang": "浙江", "hong kong": "香港", "hongkong": "香港",
    "macao": "澳门", "macau": "澳门", "taiwan": "台湾",
}

# 常见城市 → 省（快手等平台有时只给城市）
_CITY = {c: p for p, cities in {
    "广东": "广州 深圳 东莞 佛山 珠海 惠州 中山 汕头 江门 湛江 茂名 肇庆 梅州 清远 揭阳 韶关 阳江 潮州 云浮 河源 汕尾",
    "江苏": "南京 苏州 无锡 常州 南通 徐州 扬州 盐城 镇江 泰州 淮安 连云港 宿迁",
    "浙江": "杭州 宁波 温州 绍兴 嘉兴 金华 台州 湖州 衢州 丽水 舟山",
    "山东": "济南 青岛 烟台 潍坊 临沂 济宁 淄博 泰安 威海 德州 聊城 菏泽 枣庄 滨州 日照",
    "河南": "郑州 洛阳 开封 南阳 新乡 安阳 许昌 平顶山 周口 商丘 信阳 驻马店",
    "湖北": "武汉 宜昌 襄阳 荆州 黄冈 孝感 十堰",
    "湖南": "长沙 株洲 湘潭 衡阳 岳阳 常德 邵阳 益阳 郴州 永州 怀化 娄底 张家界",
    "四川": "成都 绵阳 德阳 南充 宜宾 泸州 乐山 达州 自贡 遂宁",
    "福建": "福州 厦门 泉州 漳州 莆田 宁德 龙岩 三明 南平",
    "安徽": "合肥 芜湖 蚌埠 阜阳 淮南 马鞍山 安庆 宿州",
    "河北": "石家庄 唐山 保定 邯郸 廊坊 沧州 邢台 秦皇岛 张家口 承德 衡水",
    "辽宁": "沈阳 大连 鞍山 抚顺 锦州 营口",
    "陕西": "西安 咸阳 宝鸡 渭南 汉中 榆林",
    "江西": "南昌 赣州 九江 上饶 宜春 吉安 抚州",
    "广西": "南宁 桂林 柳州 玉林 梧州 北海",
    "云南": "昆明 大理 曲靖 玉溪 丽江",
    "山西": "太原 大同 运城 临汾 长治",
    "黑龙江": "哈尔滨 大庆 齐齐哈尔 牡丹江",
    "吉林": "长春 吉林市 四平 延边",
    "贵州": "贵阳 遵义 六盘水",
    "海南": "海口 三亚",
    "新疆": "乌鲁木齐 喀什 伊犁",
    "甘肃": "兰州 天水",
    "内蒙古": "呼和浩特 包头 鄂尔多斯 赤峰",
}.items() for c in cities.split()}

_PREFIX = re.compile(r"^\s*(?:IP\s*属地|IP\s*位置|来自|发布于|属地)\s*[:：]?\s*", re.I)
_SUFFIX = re.compile(r"(壮族自治区|回族自治区|维吾尔自治区|特别行政区|自治区|省|市)$")
_ALL = sorted(PROVINCES, key=len, reverse=True)


def normalize_region(raw):
    """返回省级中文名；国外返回"海外"；没有地区信息返回空字符串。"""
    s = _PREFIX.sub("", str(raw or "")).strip()
    if not s or s in ("未知", "unknown", "Unknown", "-"):
        return ""
    low = s.lower().strip()
    if low in _EN:
        return _EN[low]
    s = s.replace("中国", "", 1) if s.startswith("中国") and len(s) > 2 else s
    s = _SUFFIX.sub("", s)
    if s in PROVINCES:
        return s
    for p in _ALL:
        if s.startswith(p):
            return p
    base = _SUFFIX.sub("", s)
    if base in _CITY:
        return _CITY[base]
    for c, p in _CITY.items():
        if s.startswith(c):
            return p
    return OVERSEAS


def region_ok(province, regions_csv, keep_unknown=True):
    """线索是否符合任务选的地区。没选地区 = 不限。"""
    wanted = {r for r in (regions_csv or "").split(",") if r}
    if not wanted:
        return True
    if not province:
        return bool(keep_unknown)
    return province in wanted
