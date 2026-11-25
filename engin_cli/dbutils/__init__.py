# -*- coding: utf-8 -*-
"""dbutils：MySQL 访问组件（可选 SSH 隧道），给「写入数据源」以及以后要连外部库的地方共用。

不依赖本项目其它模块；只要 pymysql，开 SSH 隧道时再要 paramiko，传 DataFrame 时再要 pandas。

    from engin_cli.dbutils import Database, DataSourceConfig, SSHConfig

    cfg = DataSourceConfig(host="rm-xxx.mysql.rds.aliyuncs.com", port=3306, user="u", password="p",
                           database="dw",
                           ssh=SSHConfig(host="10.1.1.8", port=22, user="mysql_connector",
                                         private_key=open("/data/key.pem").read()))   # 不走隧道就不给 ssh

    with Database(cfg) as db:                        # 用完自动断库、关隧道
        db.ping()                                    # {"version": ..., "database": ..., "via": ...}
        rows = db.select("t_user", where={"status": 1, "city": ["杭州", "上海"]}, order_by="id desc", limit=10)
        n = db.count("t_user", where={"deleted_at": None})
        db.insert("t_user", [{"name": "a"}, {"name": "b"}])
        db.upsert("t_user", rows)                    # 撞唯一键就更新
        db.update("t_user", {"status": 0}, where={"id": 3})
        db.delete("t_user", where={"id": [4, 5]})    # 不给 where 直接报错，防手滑删全表
        db.replace_rows("t_kpi", df, where={"scenic_id": ["S1"], "travel_date": [20261101]})
                                                     # 先清理再写入，同一事务，失败回滚
        with db.transaction():                       # 多步操作要么都成要么都不成
            db.execute("update ...", [...])
            db.insert(...)

模块
----
· config.py    DataSourceConfig / SSHConfig（from_dict 认「写入数据源」页面存的格式）
· tunnel.py    SSHTunnel：paramiko 本地端口转发；连不上 / 认证不过 / 密钥不对都给一句人话（TunnelError）
· database.py  Database：连接、查询、增删改、先删后写、事务、元数据
· sql.py       标识符转义、WHERE 拼装、值清洗（NaN → NULL、numpy → 原生类型）

异常
----
· ConnectError（ConnectionError 子类）：连不上（含 TunnelError）。调用方据此判断「连不上就别再试了」。
· 其余 SQL 错误原样抛 pymysql 的异常。
"""

from .config import DataSourceConfig, SSHConfig
from .database import Database
from .errors import ConnectError, TunnelError
from .sql import build_where, quote_ident
from .tunnel import SSHTunnel, key_fingerprint, load_private_key

__all__ = ["Database", "DataSourceConfig", "SSHConfig", "SSHTunnel", "ConnectError", "TunnelError",
           "build_where", "quote_ident", "key_fingerprint", "load_private_key"]
