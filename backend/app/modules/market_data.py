"""One fixed, explicitly fictional primary offering. No external price feed."""
SECTORS = [
    ('新能源', ['曦光储能', '风禾动力', '青曜电池', '海岚风能', '赤峰光伏', '星原充电']),
    ('科技', ['云帆计算', '微澜芯片', '辰序软件', '知木网络', '蓝弧仪器', '织星机器人']),
    ('医疗', ['明穗制药', '远萤生物', '清禾医疗', '白露诊断', '春池健康', '杏川器械']),
    ('消费', ['雨巷茶业', '谷晴食品', '锦芽服饰', '青柚家居', '南灯零售', '暖舟生活']),
    ('制造', ['铸川精工', '砺石机床', '千帆装备', '青山材料', '飞鹭电机', '银湾造船']),
    ('文娱', ['云鲸影业', '纸鸢出版', '星桥游戏', '听澜音乐', '墨海动画', '夏港演艺']),
    ('物流', ['顺岚运输', '远丘仓储', '蓝鲸港务', '飞舟配送', '山禾冷链', '云鹭航运']),
    ('农业', ['丰屿种业', '禾青农机', '谷雨果园', '翠川牧业', '沐田渔业', '青湾园艺']),
    ('环保', ['净澜水务', '绿星循环', '清岳环保', '森洲生态', '碧川过滤', '雨杉净化']),
    ('旅游', ['云栈酒店', '星湾旅行', '青岭文旅', '湖光露营', '白帆休闲', '远风山庄']),
]
COMPANIES = [{'code': f'MD{i+1:03}', 'name': name, 'sector': sector, 'supply': 1000, 'issue_price': (10, 15, 20, 25, 30)[i % 5]}
             for i, (sector, name) in enumerate((sector, n) for sector, names in SECTORS for n in names)]
DEFAULTS = {'fee_bps': 0, 'recommended_codes': '', 'digest_enabled': False,
            'digest_times': '10:00,18:00', 'ipo_limit': 50, 'holding_limit': 200, 'order_quantity': 100,
            'max_price': 1000, 'max_notional': 5000, 'max_orders': 20, 'order_hours': 24,
            'ipo_enabled': True, 'trading_enabled': True}


def fee(gross, bps):
    return (gross*bps+9999)//10000
