import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "leads.db")

# Defaults written to the settings table on first run; edit them on the 设置 page.
DEFAULT_SETTINGS = {
    # 数据接口的地址和 Key 都不随源码提供（向作者获取）：用环境变量首次写入，或登录后在「管理后台 → 系统设置」里填写
    "api_key": os.environ.get("DATA_API_KEY", ""),
    "api_base": os.environ.get("DATA_API_BASE", ""),
    "intent_strong": "\n".join([
        "想了解", "了解一下", "怎么联系", "联系方式", "怎么合作", "想合作", "求合作",
        "想加盟", "怎么加盟", "加盟费", "想投资", "怎么投资", "想做", "我也想",
        "怎么买", "哪里买", "在哪买", "想买", "求链接", "求购", "怎么报名",
        "感兴趣", "有兴趣", "加微信", "电话多少", "怎么找你", "私聊", "带带我",
    ]),
    "intent_medium": "\n".join([
        "多少钱", "价格", "报价", "费用", "怎么收费", "贵不贵", "成本", "回本", "利润",
        "赚钱吗", "能赚", "靠谱吗", "靠谱", "效果怎么样", "好用吗", "售后",
        "在哪", "哪里有", "怎么弄", "怎么做", "有没有", "考虑", "咨询", "私信",
    ]),
    "intent_exclude": "\n".join([
        "私信我", "加我", "找我", "滴滴我", "厂家直销", "我们公司", "招代理", "诚招",
    ]),
    # 计费：额度的单位就是「条评论」，采集时按新抓到的评论条数扣。微信类（视频号 / 公众号）每条按下面的倍数扣
    "wechat_weight": "2",
    # 对标账号：每刷新一次（自动或手动）扣多少条额度
    "price_track": "20",
    # 自定义金额充值时，1 元 = 多少条评论额度
    "comments_per_yuan": "100",
    # 给别人发接口 Key 的批发价：每 1000 条评论多少钱（后台「接口 Key」页用来「金额 ⇄ 条数」自动换算）
    "gateway_price_per_k": "10",
    # AI 仿写用的是客户自己的阿里云 Key（填在各自的「我的账户」里），这里只放默认模型名。
    "ai_text_model": "qwen-plus",
    "ai_image_model": "qwen-image-2.0",
    "ai_video_model": "wan2.7",
    # open = 注册即可用；approval = 注册后需管理员审核；closed = 关闭注册
    "register_mode": "approval",
    "register_bonus": "0",
    # 充值套餐：每行「付款金额（元） 到账评论条数」，用户在充值页选择；收款码图片存在 media/pay-*.png|jpg
    "recharge_packages": "50 5000\n100 10000\n300 30000\n1000 100000",
    "recharge_contact": "",   # 联系方式（微信号等），显示在充值页
    "recharge_note": "",      # 充值页补充说明
}

COMMENTS_PER_TIER = 50
