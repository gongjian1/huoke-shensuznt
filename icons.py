"""Hand-drawn SVG icon set (24-unit grid, 1.5 stroke) and the animated logo mark.

Icons are emitted inline (not via <use>) so CSS can animate their inner parts on hover.
"""
from markupsafe import Markup

_ICONS = {
    # 仪表盘：坐标轴 + 三根柱
    "dash": '<path d="M3.5 3.5v17h17"/>'
            '<rect class="f b b1" x="7" y="12.5" width="3" height="5"/>'
            '<rect class="f b b2" x="12" y="9" width="3" height="8.5"/>'
            '<rect class="f b b3" x="17" y="5.5" width="3" height="12"/>',
    # 采集：雷达
    "scan": '<circle cx="12" cy="12" r="8.5"/>'
            '<circle class="dim" cx="12" cy="12" r="4.25"/>'
            '<g class="sweep"><path class="wedge" d="M12 12V3.5A8.5 8.5 0 0 0 6 6z"/><path d="M12 12V3.5"/></g>'
            '<rect class="f blip" x="15.2" y="7.6" width="2" height="2"/>',
    # 线索：锁定框 + 人
    "target": '<g class="brk"><path class="c1" d="M3.5 8V3.5H8"/><path class="c2" d="M16 3.5h4.5V8"/>'
              '<path class="c3" d="M20.5 16v4.5H16"/><path class="c4" d="M8 20.5H3.5V16"/></g>'
              '<circle cx="12" cy="10" r="2.75"/>'
              '<path d="M7.5 17.5c.9-2.4 2.6-3.5 4.5-3.5s3.6 1.1 4.5 3.5"/>',
    # 作品库：屏幕 + 播放 + 进度条
    "play": '<rect x="3.5" y="4.5" width="17" height="15"/>'
            '<path class="f tri" d="M10.25 8.25v5.5l4.5-2.75z"/>'
            '<path class="dim" d="M6.5 16.5h11"/>'
            '<path class="prog" d="M6.5 16.5h4"/>',
    # AI 视频：场记板 + 播放三角
    "film": '<path d="M3.5 9.5h17v10h-17z"/>'
            '<path class="dim" d="M3.5 9.5L6 4.5M9 9.5l2.5-5M14.5 9.5L17 4.5M20.5 9.5v-5"/>'
            '<path class="f tri" d="M10.5 12.5v4l3.5-2z"/>',
    # 私信话术：对话气泡
    "send": '<path d="M4.5 5.5h15v10.5h-7.5l-4 3.5V16h-3.5z"/>'
            '<path class="dim" d="M8 9.25h8M8 12.5h5"/>',
    # 设置：三条推子
    "sliders": '<path class="dim" d="M3.5 7h17M3.5 12h17M3.5 17h17"/>'
               '<rect class="f k k1" x="6.5" y="5" width="3.5" height="4"/>'
               '<rect class="f k k2" x="14" y="10" width="3.5" height="4"/>'
               '<rect class="f k k3" x="9" y="15" width="3.5" height="4"/>',
    "logout": '<path d="M13.5 4h6v16h-6"/>'
              '<g class="arr"><path d="M15.5 12H4.5"/><path d="M8 8.5 4.5 12 8 15.5"/></g>',
    "download": '<path d="M4 14.5V20h16v-5.5"/>'
                '<g class="arr"><path d="M12 3.5v11"/><path d="M8 10.5l4 4 4-4"/></g>',
    "plus": '<g class="rot"><path d="M12 4.5v15M4.5 12h15"/></g>',
    "arrow": '<g class="arr"><path d="M4.5 12h14.5"/><path d="M13.5 6.5 19 12l-5.5 5.5"/></g>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 20.5 20.5"/>',
    "filter": '<path d="M3.5 5h17l-6.5 7.5V19l-4 1.5v-8z"/>',
    "refresh": '<g class="rot"><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 3.5V8H15"/></g>',
    "stop": '<rect x="4" y="4" width="16" height="16"/><rect class="f" x="9" y="9" width="6" height="6"/>',
    "external": '<path d="M13.5 4.5h6v6M19.5 4.5l-8 8M17.5 14v5.5h-13v-13H10"/>',
    "wallet": '<rect x="3.5" y="6" width="17" height="13"/><path d="M3.5 9.5h17"/>'
              '<rect class="f" x="14.5" y="12.75" width="3" height="3"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="10"/>'
            '<path class="shackle" d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3"/><path d="M12 14v3"/>',
    "key": '<circle cx="8" cy="15.5" r="4.5"/><path d="M11.3 12.2 20 3.5M16 7.5l3 3M13.5 10l2 2"/>',
    "ping": '<rect class="f" x="10.5" y="10.5" width="3" height="3"/>'
            '<path class="w1" d="M7.75 7.75a6 6 0 0 0 0 8.5M16.25 7.75a6 6 0 0 1 0 8.5"/>'
            '<path class="w2" d="M4.9 4.9a10 10 0 0 0 0 14.2M19.1 4.9a10 10 0 0 1 0 14.2"/>',
    "check": '<path d="M4.5 12.5l4.5 4.5 10.5-10.5"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.66 0l3-3A4 4 0 0 0 13 5.34l-1 1"/>'
            '<path d="M14 10a4 4 0 0 0-5.66 0l-3 3A4 4 0 0 0 11 18.66l1-1"/>',
    "list": '<path d="M9 6h11.5M9 12h11.5M9 18h11.5"/>'
            '<rect class="f" x="3.5" y="5" width="2.5" height="2.5"/>'
            '<rect class="f" x="3.5" y="11" width="2.5" height="2.5"/>'
            '<rect class="f" x="3.5" y="17" width="2.5" height="2.5"/>',
    "grid": '<rect x="3.5" y="3.5" width="7" height="7"/><rect x="13.5" y="3.5" width="7" height="7"/>'
            '<rect x="3.5" y="13.5" width="7" height="7"/><rect class="f" x="13.5" y="13.5" width="7" height="7"/>',
    "tag": '<path d="M3.5 3.5h8l9 9-8 8-9-9z"/><rect class="f" x="7" y="7" width="2.5" height="2.5"/>',
    "user": '<circle cx="12" cy="8.5" r="3.75"/><path d="M4.5 20.5c1.2-3.8 4-5.5 7.5-5.5s6.3 1.7 7.5 5.5"/>',
    "user-plus": '<circle cx="10" cy="8.5" r="3.75"/><path d="M3 20.5c1.2-3.8 3.7-5.5 7-5.5 1.5 0 2.8.3 3.9 1"/>'
                 '<g class="rot"><path d="M18.5 14v6M15.5 17h6"/></g>',
    "shield": '<path d="M12 3.5 19.5 6v6c0 4.5-3.2 7.4-7.5 8.5C7.7 19.4 4.5 16.5 4.5 12V6z"/>'
              '<path class="chk" d="M8.75 12l2.25 2.25 4.25-4.5"/>',
    "coin": '<path d="M5 7.5v4.5c0 1.5 3.1 2.75 7 2.75s7-1.25 7-2.75V7.5"/>'
            '<path d="M5 12v4.5c0 1.5 3.1 2.75 7 2.75s7-1.25 7-2.75V12"/>'
            '<ellipse class="top" cx="12" cy="7.5" rx="7" ry="2.75"/>',
    "phone": '<rect x="6.5" y="2.5" width="11" height="19"/><path d="M10.5 18.5h3"/>',
    "users": '<circle cx="9" cy="8.5" r="3.25"/><path d="M3 19.5c.9-3.2 3.1-4.75 6-4.75s5.1 1.55 6 4.75"/>'
             '<path d="M15.5 5.5a3.25 3.25 0 0 1 0 6.25M17.5 14.9c1.7.6 2.9 2.1 3.5 4.6"/>',
    "receipt": '<path d="M5.5 3.5h13v17l-2.2-1.5-2.1 1.5-2.2-1.5-2.1 1.5-2.2-1.5-2.2 1.5z"/>'
               '<path class="ln" d="M8.5 8h7M8.5 11.5h7M8.5 15h4"/>',
    # 对标：瞄准镜里的眼睛
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/>'
           '<g class="pupil"><circle cx="12" cy="12" r="3"/><rect class="f" x="11" y="11" width="2" height="2"/></g>',
    "trend": '<path class="ln" d="M3.5 17.5l6-6 4 4 7-7.5"/><path d="M15 8h5.5v5.5"/>',
    "pause": '<path d="M8.5 5v14M15.5 5v14"/>',
    "resume": '<path class="f" d="M7.5 4.5v15l12-7.5z"/>',
    "trash": '<path d="M4.5 6.5h15M9.5 6.5V4h5v2.5"/><path d="M6.5 6.5l1 13.5h9l1-13.5"/><path class="dim" d="M10 10v6.5M14 10v6.5"/>',
    # AI：四角星 + 小十字
    "spark": '<path class="big" d="M10.5 4.5l1.9 5.6 5.6 1.9-5.6 1.9-1.9 5.6-1.9-5.6L3 12l5.6-1.9z"/><path class="small" d="M19 3v4M17 5h4"/>',
    "copy": '<rect x="8.5" y="8.5" width="12" height="12"/><path class="back" d="M15.5 8.5v-5h-12v12h5"/>',
    "image": '<rect x="3.5" y="4.5" width="17" height="15"/><circle class="f sun" cx="9" cy="10" r="1.6"/><path class="hill" d="M3.5 17l5-4.5 4 3.5 3-2.5 5 4"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><g class="hand"><path d="M12 7v5l3.5 2"/></g>',
}


def icon(name, cls=""):
    return Markup(f'<svg class="i i-{name} {cls}" viewBox="0 0 24 24" aria-hidden="true">{_ICONS[name]}</svg>')


def logo(cls=""):
    return Markup(
        f'<svg class="logo {cls}" viewBox="0 0 32 32" aria-hidden="true">'
        '<circle class="logo-ring" cx="16" cy="16" r="10.5"/>'
        '<path class="logo-brk tl" d="M1.5 9V1.5H9"/>'
        '<path class="logo-brk tr" d="M23 1.5h7.5V9"/>'
        '<path class="logo-brk br" d="M30.5 23v7.5H23"/>'
        '<path class="logo-brk bl" d="M9 30.5H1.5V23"/>'
        '<path class="logo-cross" d="M16 5v3.5M16 23.5V27M5 16h3.5M23.5 16H27"/>'
        '<rect class="logo-core" x="13" y="13" width="6" height="6"/>'
        '</svg>'
    )
