import sqlite3
import json
import re
import sys

from _ctypes import PyObj_FromPtr

class NoIndent(object):
    def __init__(self, value):
        if not isinstance(value, (list, tuple)):
            raise TypeError('Only lists and tuples can be wrapped')
        self.value = value

class MyEncoder(json.JSONEncoder):
    FORMAT_SPEC = '@@{}@@'
    regex = re.compile(FORMAT_SPEC.format(r'(\d+)'))

    def __init__(self, **kwargs):
        ignore = {'cls', 'indent'}
        self._kwargs = {k: v for k, v in kwargs.items() if k not in ignore}
        super(MyEncoder, self).__init__(**kwargs)

    def default(self, obj):
        return (self.FORMAT_SPEC.format(id(obj)) if isinstance(obj, NoIndent)
                    else super(MyEncoder, self).default(obj))

    def iterencode(self, obj, **kwargs):
        format_spec = self.FORMAT_SPEC
        for encoded in super(MyEncoder, self).iterencode(obj, **kwargs):
            match = self.regex.search(encoded)
            if match:
                id = int(match.group(1))
                no_indent = PyObj_FromPtr(id)
                json_repr = json.dumps(no_indent.value, **self._kwargs)
                encoded = encoded.replace(
                            '"{}"'.format(format_spec.format(id)), json_repr)
            yield encoded

def get_table_columns(db_path, table_name):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name});")
    columns = [(row[1], row[2]) for row in cursor.fetchall()]
    conn.close()
    return columns

def get_primary_key_column(db_path, table_name):

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(f"PRAGMA table_info({table_name});")
    columns_info = cursor.fetchall()
    
    for column in columns_info:
        if column[5] == 1:
            conn.close()
            return column[1]

    cursor.execute(f"PRAGMA index_list({table_name});")
    indexes = cursor.fetchall()
    
    for index in indexes:
        if index[2] == 1:
            cursor.execute(f"PRAGMA index_info({index[1]});")
            index_columns = cursor.fetchall()
            if len(index_columns) == 1:
                unique_column = index_columns[0][2]
                conn.close()
                return unique_column
    
    cursor.execute(f"SELECT * FROM {table_name}")
    columns = [description[0] for description in cursor.description]
    
    for column in columns:
        cursor.execute(f"SELECT COUNT(DISTINCT {column}) = COUNT(*) FROM {table_name}")
        is_unique = cursor.fetchone()[0]
        
        if is_unique:
            conn.close()
            return column
    
    conn.close()
    return None

def get_table_data(db_path, table_name):

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(f"PRAGMA table_info({table_name});")
    column_names = [row[1] for row in cursor.fetchall()]
    
    quoted_column_names = [f'"{col}"' for col in column_names]
    
    select_query = f"SELECT {', '.join(quoted_column_names)} FROM {table_name}"
    cursor.execute(select_query)
    
    rows = cursor.fetchall()
    
    data = []
    for row in rows:
        row_dict = dict(zip(column_names, row))
        data.append(row_dict)
    
    conn.close()
    return set(tuple(sorted(d.items())) for d in data)

def analyze_table_changes(db_path, table_name, data1, data2):

    if not data1 and not data2:
        return {
            'added': [],
            'deleted': [],
            'modified': []
        }
    
    id_key = get_primary_key_column(db_path, table_name)
    
    if not id_key:
        print(f"Warning: Could not find unique identifier for table {table_name}")
        return {
            'WARNING': "Could not find unique identifier for table"
        }

    if data1 and data2:
        common_columns = set([i[0] for i in list(data1)[0]]) & set([i[0] for i in list(data2)[0]])
    elif data1:
        common_columns = set([i[0] for i in list(data1)[0]])
    elif data2:
        common_columns = set([i[0] for i in list(data2)[0]])
    else:
        common_columns = set()
    
    if id_key in common_columns:
        common_columns.remove(id_key)
    
    data1_dict = {[i[1] for i in item if i[0] == id_key][0]: item for item in data1 if id_key in [i[0] for i in item]}
    data2_dict = {[i[1] for i in item if i[0] == id_key][0]: item for item in data2 if id_key in [i[0] for i in item]}

    
    changes = {
        'added': [],
        'deleted': [],
        'modified': []
    }
    
    changes['added'] = [
        NoIndent(record) for key, record in data2_dict.items() 
        if key not in data1_dict
    ]
    
    changes['deleted'] = [
        NoIndent(record) for key, record in data1_dict.items() 
        if key not in data2_dict
    ]
    
    for key, record1 in data1_dict.items():
        if key in data2_dict:
            record2 = data2_dict[key]

            record1_compare = {k: v for k, v in record1 if k in common_columns}
            record2_compare = {k: v for k, v in record2 if k in common_columns}
            
            if record1_compare != record2_compare:
                changes['modified'].append({
                    'id': key,
                    'old': NoIndent(record1),
                    'new': NoIndent(record2)
                })
    
    return changes

def compare_databases(db1_path, db2_path):

    conn1 = sqlite3.connect(db1_path)
    conn2 = sqlite3.connect(db2_path)
    cursor1 = conn1.cursor()
    cursor2 = conn2.cursor()


    cursor1.execute("SELECT name FROM sqlite_master WHERE type='table';")
    cursor2.execute("SELECT name FROM sqlite_master WHERE type='table';")


    tables1 = {row[0] for row in cursor1.fetchall()}
    tables2 = {row[0] for row in cursor2.fetchall()}

    detailed_changes = {}

    only_in_db1 = tables1 - tables2
    only_in_db2 = tables2 - tables1
    
    detailed_changes['tables'] = {
        'only_in_old_db': list(only_in_db1),
        'only_in_new_db': list(only_in_db2)
    }

    common_tables = tables1 & tables2
    detailed_changes['table_changes'] = {}


    for table in common_tables:

        columns1 = get_table_columns(db1_path, table)
        columns2 = get_table_columns(db2_path, table)

        column_names1 = {col[0] for col in columns1}
        column_names2 = {col[0] for col in columns2}

        table_changes = {}

        if column_names1 != column_names2:
            table_changes['column_differences'] = {
                'only_in_db1': list(column_names1 - column_names2),
                'only_in_db2': list(column_names2 - column_names1)
            }

        if column_names1 == column_names2:
            different_column_types = []
            for col1, col2 in zip(
                sorted(columns1, key=lambda x: x[0]), 
                sorted(columns2, key=lambda x: x[0])
            ):
                if col1[1] != col2[1]:
                    different_column_types.append({
                        'column': col1[0],
                        'old_type': col1[1],
                        'new_type': col2[1]
                    })
            
            if different_column_types:
                table_changes['column_type_changes'] = different_column_types

        data1 = get_table_data(db1_path, table)
        data2 = get_table_data(db2_path, table)

        data_changes = analyze_table_changes(db1_path, table, data1, data2)
        
        if any(data_changes.values()):
            table_changes['data_changes'] = {k: v for k, v in data_changes.items() if v}

        if table_changes:
            detailed_changes['table_changes'][table] = table_changes

    with open('detailed_changes.json', 'w', encoding='utf-8') as f:
        json.dump(detailed_changes, f, cls=MyEncoder, ensure_ascii=False, indent=2)

    conn1.close()
    conn2.close()

    return detailed_changes

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python compare_databases.py latest_db.db previous_db.db")
        sys.exit(1)

    db1_path = sys.argv[1]
    db2_path = sys.argv[2]
    compare_databases(db1_path, db2_path)
