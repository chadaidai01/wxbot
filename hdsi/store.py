# -*- coding: utf-8 -*-
"""SQLite 存储层：复刻上游 Koishi ctx.database 在本插件里用到的子集。

上游 src/service.ts 只使用四个入口：
    dbGet(table, query, options) / dbCreate(table, data) / dbSet(table, query, data) / dbRemove(table, query)
本模块提供同语义的 Database，并负责：
- 建表（src/database.ts 的表结构）；
- Date ↔ ISO 文本、JSON ↔ TEXT、bool ↔ INTEGER 编解码；
- 查询支持等值与 $in/$gt/$gte/$lt/$lte，options 支持 limit/sort；
- 行读出时执行 normalize_database_row（story.state 解码等）。
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .database import DATABASE_DATE_FIELDS, JSON_FIELDS, SQLITE_TYPES, TABLES, table_spec
from .time_utils import parse_time


def _to_iso(value: Any) -> Optional[str]:
    parsed = parse_time(value)
    if parsed is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime('%Y-%m-%dT%H:%M:%S.') + f'{parsed.microsecond // 1000:03d}Z'


def _from_iso(value: Any) -> Optional[datetime]:
    return parse_time(value)


def _encode_value(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == 'timestamp':
        return _to_iso(value)
    if kind == 'json':
        return json.dumps(value, ensure_ascii=False)
    if kind == 'boolean':
        return 1 if value else 0
    if kind == 'unsigned':
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if kind == 'double':
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if kind in ('string', 'text'):
        return value if isinstance(value, str) else str(value)
    return value


def _decode_value(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == 'timestamp':
        return _from_iso(value)
    if kind == 'json':
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return None
        return value
    if kind == 'boolean':
        return bool(value)
    if kind == 'unsigned':
        return int(value)
    if kind == 'double':
        return float(value)
    return value


class Database:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=NORMAL')
            self._create_tables()
            self._conn.commit()

    # ---------- schema ----------
    def _create_tables(self) -> None:
        for table, spec in TABLES.items():
            columns = []
            for field, kind in spec['fields'].items():
                if spec.get('autoInc') and field == spec['primary']:
                    columns.append(f'"{field}" INTEGER PRIMARY KEY AUTOINCREMENT')
                    continue
                column = f'"{field}" {SQLITE_TYPES.get(kind, "TEXT")}'
                if field == spec['primary']:
                    column += ' PRIMARY KEY'
                columns.append(column)
            unique = spec.get('unique') or []
            if unique:
                columns.append('UNIQUE (' + ', '.join(f'"{name}"' for name in unique) + ')')
            self._conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(columns)})')
            for index in spec.get('indexes') or []:
                self._conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "idx_{table}_{index}" ON "{table}" ("{index}")'
                )

    # ---------- public API ----------
    def get(self, table: str, query: Optional[Dict[str, Any]] = None, options: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return self._read(lambda: self._get(table, query or {}, options or {}))

    def create(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return self._write(lambda: self._create(table, data))

    def set(self, table: str, query: Dict[str, Any], data: Dict[str, Any]) -> None:
        return self._write(lambda: self._set(table, query, data))

    def remove(self, table: str, query: Dict[str, Any]) -> None:
        return self._write(lambda: self._remove(table, query))

    def upsert(self, table: str, data: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
        query = {key: data.get(key) for key in keys}
        existing = self.get(table, query)
        if existing:
            self.set(table, query, {k: v for k, v in data.items() if k not in keys})
            return self.get(table, query)[0]
        return self.create(table, data)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- internals ----------
    def _read(self, task):
        return self._with_retry(task, write=False)

    def _write(self, task):
        return self._with_retry(task, write=True)

    def _with_retry(self, task, write: bool):
        delays = [0.1, 0.25, 0.5, 1.0, 2.0] if write else [0.05, 0.125, 0.25]
        attempt = 0
        while True:
            try:
                with self._lock:
                    result = task()
                    self._conn.commit()
                    return result
            except sqlite3.OperationalError as error:
                if attempt >= len(delays) or not _is_transient(error):
                    raise
                time.sleep(delays[attempt] + random.random() * 0.05)
                attempt += 1

    def _where(self, table: str, query: Dict[str, Any]):
        spec = table_spec(table)
        fields = spec['fields']
        clauses: List[str] = []
        params: List[Any] = []
        for key, value in query.items():
            if key not in fields:
                continue
            kind = fields[key]
            if isinstance(value, dict):
                for operator, operand in value.items():
                    if operator == '$in':
                        values = list(operand or [])
                        if not values:
                            clauses.append('0 = 1')
                            continue
                        placeholders = ', '.join('?' for _ in values)
                        clauses.append(f'"{key}" IN ({placeholders})')
                        params.extend(_encode_value(kind, item) for item in values)
                    elif operator in ('$gt', '$gte', '$lt', '$lte'):
                        symbol = {'$gt': '>', '$gte': '>=', '$lt': '<', '$lte': '<='}[operator]
                        clauses.append(f'"{key}" {symbol} ?')
                        params.append(_encode_value(kind, operand))
                    elif operator == '$ne':
                        clauses.append(f'("{key}" IS NULL OR "{key}" != ?)')
                        params.append(_encode_value(kind, operand))
                    else:
                        raise ValueError(f'unsupported query operator: {operator}')
            elif value is None:
                clauses.append(f'"{key}" IS NULL')
            else:
                clauses.append(f'"{key}" = ?')
                params.append(_encode_value(kind, value))
        return (' AND '.join(clauses) if clauses else '1 = 1'), params

    def _get(self, table: str, query: Dict[str, Any], options: Dict[str, Any]) -> List[Dict[str, Any]]:
        where, params = self._where(table, query)
        sql = f'SELECT * FROM "{table}" WHERE {where}'
        sort = options.get('sort') or {}
        if sort:
            orders = []
            for field, direction in sort.items():
                if field not in table_spec(table)['fields']:
                    continue
                orders.append(f'"{field}" {"DESC" if str(direction).lower() == "desc" else "ASC"}')
            if orders:
                sql += ' ORDER BY ' + ', '.join(orders)
        limit = options.get('limit')
        offset = options.get('offset')
        if isinstance(limit, int) and limit >= 0:
            sql += ' LIMIT ?'
            params.append(limit)
            if isinstance(offset, int) and offset > 0:
                sql += ' OFFSET ?'
                params.append(offset)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._normalize_row(table, dict(row)) for row in rows]

    def _create(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        spec = table_spec(table)
        fields = spec['fields']
        row = {key: value for key, value in data.items() if key in fields and value is not None}
        if spec.get('autoInc'):
            row.pop(spec['primary'], None)
        columns = list(row.keys())
        values = [_encode_value(fields[column], row[column]) for column in columns]
        placeholders = ', '.join('?' for _ in columns)
        column_sql = ', '.join('"' + column + '"' for column in columns)
        sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (table, column_sql, placeholders)
        cursor = self._conn.execute(sql, values)
        if spec.get('autoInc'):
            new_id = cursor.lastrowid
        else:
            new_id = row.get(spec['primary'])
        created = self._conn.execute(
            f'SELECT * FROM "{table}" WHERE "{spec["primary"]}" = ?', (new_id,)
        ).fetchone()
        return self._normalize_row(table, dict(created) if created else dict(row))

    def _set(self, table: str, query: Dict[str, Any], data: Dict[str, Any]) -> None:
        fields = table_spec(table)['fields']
        patch = {key: value for key, value in data.items() if key in fields}
        if not patch:
            return
        assignments = []
        params: List[Any] = []
        for key, value in patch.items():
            assignments.append(f'"{key}" = ?')
            params.append(_encode_value(fields[key], value))
        where, where_params = self._where(table, query)
        params.extend(where_params)
        self._conn.execute(f'UPDATE "{table}" SET {", ".join(assignments)} WHERE {where}', params)

    def _remove(self, table: str, query: Dict[str, Any]) -> None:
        where, params = self._where(table, query)
        self._conn.execute(f'DELETE FROM "{table}" WHERE {where}', params)

    def count(self, table: str, query: Optional[Dict[str, Any]] = None) -> int:
        where, params = self._where(table, query or {})
        row = self._conn.execute(f'SELECT COUNT(*) AS n FROM "{table}" WHERE {where}', params).fetchone()
        return int(row['n']) if row else 0

    def _normalize_row(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        fields = table_spec(table)['fields']
        for field, value in list(row.items()):
            kind = fields.get(field)
            if kind:
                row[field] = _decode_value(kind, value)
        return normalize_database_row(table, row)


def _is_transient(error: Exception) -> bool:
    text = str(error).lower()
    return any(token in text for token in ('disk i/o', 'database is locked', 'busy', 'unable to open'))


def normalize_database_row(table: str, value: Dict[str, Any]) -> Dict[str, Any]:
    """对应上游 service.ts 的 normalizeDatabaseRow。"""
    row = dict(value)
    for field in DATABASE_DATE_FIELDS.get(table) or []:
        if row.get(field) is None:
            continue
        row[field] = parse_time(row[field])
    if table == 'interlude_story':
        from .story_state import decode_story_state
        created_at = row.get('createdAt') or datetime.now(timezone.utc)
        updated_at = row.get('updatedAt') or created_at
        row['createdAt'] = created_at
        row['updatedAt'] = updated_at
        row['cursorAt'] = row.get('cursorAt') or updated_at
        row['state'] = decode_story_state(row.get('state'))
    elif table == 'interlude_participant':
        from .story_state import normalize_participant_state
        created_at = row.get('createdAt') or datetime.now(timezone.utc)
        row['createdAt'] = created_at
        row['updatedAt'] = row.get('updatedAt') or created_at
        row['state'] = normalize_participant_state(row.get('state'))
    return row


_store: Optional[Database] = None
_store_lock = threading.Lock()


def get_store(path: str) -> Database:
    """进程级单例；path 变化时（测试）重新打开。"""
    global _store
    with _store_lock:
        if _store is None or _store.path != path:
            if _store is not None:
                try:
                    _store.close()
                except Exception:  # noqa: BLE001 - 关闭失败不应阻塞运行
                    pass
            _store = Database(path)
        return _store
