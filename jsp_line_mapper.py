#!/usr/bin/env python3
"""
jsp_line_mapper.py — JSP ↔ Java 行號對應工具

用途：jspc 轉譯 JSP 為 Java 後，在生成的 Java 檔案中插入原始 JSP 行號註解，
      讓 Semgrep 掃描結果可以對應回原始 JSP 檔案。

用法：python3 jsp_line_mapper.py <webapp_root> <converted_dir>

範例：python3 jsp_line_mapper.py ./src/main/webapp ./converted

適用於：GitHub Actions / GitLab CI / Azure DevOps / Jenkins / 任何有 Python 3 的環境
"""

import os
import re
import sys


def decode_jasper_name(name: str) -> str:
    """
    將 Jasper 編碼的檔名還原。
    例如：download_005fid_jsp.java → download_id.jsp
         change_002dinfo_jsp.java → change-info.jsp
    """
    # 移除 .java 副檔名
    name = name.replace(".java", "")
    # 移除 _jsp 後綴
    if name.endswith("_jsp"):
        name = name[:-4]
    # 還原編碼字元：_XXXX → 對應字元
    def replace_encoded(match):
        hex_val = match.group(1)
        return chr(int(hex_val, 16))
    name = re.sub(r"_([0-9a-fA-F]{4})", replace_encoded, name)
    return name + ".jsp"


def decode_jasper_dir(name: str) -> str:
    """還原 Jasper 編碼的目錄名稱。例如：WEB_002dINF → WEB-INF"""
    def replace_encoded(match):
        hex_val = match.group(1)
        return chr(int(hex_val, 16))
    return re.sub(r"_([0-9a-fA-F]{4})", replace_encoded, name)


def java_path_to_jsp_path(java_path: str, converted_dir: str) -> str:
    """
    從轉譯後的 Java 檔案路徑推算原始 JSP 的相對路徑。
    例如：converted/org/apache/jsp/vulnerability/sqli/download_005fid_jsp.java
        → vulnerability/sqli/download_id.jsp
    """
    # 取得相對於 converted_dir 的路徑
    rel = os.path.relpath(java_path, converted_dir)
    # 移除 org/apache/jsp/ 前綴
    parts = rel.replace("\\", "/").split("/")
    if len(parts) > 3 and parts[0] == "org" and parts[1] == "apache" and parts[2] == "jsp":
        parts = parts[3:]
    # 還原檔名和目錄名
    parts[-1] = decode_jasper_name(parts[-1])
    parts[:-1] = [decode_jasper_dir(p) for p in parts[:-1]]
    return "/".join(parts)


def extract_jsp_scriptlet_lines(jsp_path: str) -> list:
    """
    從 JSP 檔案中擷取 scriptlet 區塊內的程式碼行。
    回傳 [(jsp_line_number, stripped_content), ...]
    """
    with open(jsp_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    lines = content.split("\n")
    result = []
    in_scriptlet = False

    for i, line in enumerate(lines, 1):
        remaining = line

        while remaining:
            if not in_scriptlet:
                # 尋找 scriptlet 開始 <% （但不是 <%@ 和 <%-- ）
                match = re.search(r"<%(?!@|--)", remaining)
                if match:
                    in_scriptlet = True
                    after = remaining[match.end():]
                    # 檢查同一行是否有結束 %>
                    end = after.find("%>")
                    if end != -1:
                        code = after[:end].strip()
                        if code:
                            result.append((i, code))
                        in_scriptlet = False
                        remaining = after[end + 2:]
                    else:
                        code = after.strip()
                        if code:
                            result.append((i, code))
                        remaining = ""
                else:
                    remaining = ""
            else:
                # 在 scriptlet 內部
                end = remaining.find("%>")
                if end != -1:
                    code = remaining[:end].strip()
                    if code:
                        result.append((i, code))
                    in_scriptlet = False
                    remaining = remaining[end + 2:]
                else:
                    code = remaining.strip()
                    if code:
                        result.append((i, code))
                    remaining = ""

    return result


def add_line_mapping(java_path: str, jsp_rel_path: str, scriptlet_lines: list) -> tuple:
    """
    在 Java 檔案的對應行後面加上 JSP 行號註解。
    回傳 (修改行數, 總行數)
    """
    with open(java_path, "r", encoding="utf-8", errors="replace") as f:
        java_lines = f.readlines()

    # 建立 scriptlet 行的順序匹配佇列
    script_queue = list(scriptlet_lines)
    queue_idx = 0
    mapped_count = 0
    new_lines = []

    for java_line in java_lines:
        stripped = java_line.rstrip("\n\r")
        java_content = stripped.strip()

        # 跳過空行和已經有標記的行
        if java_content and queue_idx < len(script_queue):
            jsp_lineno, jsp_content = script_queue[queue_idx]

            # 精確匹配（忽略前後空白）
            if java_content == jsp_content:
                # 加上 JSP 行號註解
                comment = f"  // [JSP] {jsp_rel_path}:{jsp_lineno}"
                new_lines.append(stripped + comment + "\n")
                queue_idx += 1
                mapped_count += 1
                continue
            else:
                # 嘗試往前看幾行（處理 Jasper 可能插入的 out.write 等）
                lookahead = min(queue_idx + 5, len(script_queue))
                found = False
                for la in range(queue_idx + 1, lookahead):
                    if java_content == script_queue[la][1]:
                        # 跳過中間未匹配的 scriptlet 行
                        jsp_lineno = script_queue[la][1]
                        jsp_lineno_num = script_queue[la][0]
                        comment = f"  // [JSP] {jsp_rel_path}:{jsp_lineno_num}"
                        new_lines.append(stripped + comment + "\n")
                        queue_idx = la + 1
                        mapped_count += 1
                        found = True
                        break
                if found:
                    continue

        new_lines.append(java_line if java_line.endswith("\n") else java_line + "\n")

    with open(java_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    return mapped_count, len(java_lines)


def process_all(webapp_root: str, converted_dir: str):
    """主流程：處理所有轉譯後的 Java 檔案。"""
    total_files = 0
    mapped_files = 0
    total_mapped_lines = 0

    for root, dirs, files in os.walk(converted_dir):
        for fname in files:
            if not fname.endswith(".java"):
                continue

            java_path = os.path.join(root, fname)
            jsp_rel = java_path_to_jsp_path(java_path, converted_dir)
            jsp_full = os.path.join(webapp_root, jsp_rel)
            total_files += 1

            if not os.path.exists(jsp_full):
                print(f"  [SKIP] {jsp_rel} — 找不到原始 JSP")
                continue

            scriptlet_lines = extract_jsp_scriptlet_lines(jsp_full)
            if not scriptlet_lines:
                print(f"  [SKIP] {jsp_rel} — 沒有 scriptlet 程式碼")
                continue

            mapped, total = add_line_mapping(java_path, jsp_rel, scriptlet_lines)
            if mapped > 0:
                mapped_files += 1
                total_mapped_lines += mapped
                print(f"  [OK]   {jsp_rel} — {mapped} 行已標記")
            else:
                print(f"  [WARN] {jsp_rel} — 無法對應行號")

    print(f"\n=== 行號對應完成 ===")
    print(f"處理檔案：{total_files}")
    print(f"成功對應：{mapped_files} 檔案，{total_mapped_lines} 行")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"用法：python3 {sys.argv[0]} <webapp_root> <converted_dir>")
        print(f"範例：python3 {sys.argv[0]} ./src/main/webapp ./converted")
        sys.exit(1)

    webapp_root = sys.argv[1]
    converted_dir = sys.argv[2]

    if not os.path.isdir(webapp_root):
        print(f"錯誤：webapp 目錄不存在：{webapp_root}")
        sys.exit(1)
    if not os.path.isdir(converted_dir):
        print(f"錯誤：轉譯目錄不存在：{converted_dir}")
        sys.exit(1)

    print(f"JSP 行號對應工具")
    print(f"JSP 目錄：{webapp_root}")
    print(f"Java 目錄：{converted_dir}\n")
    process_all(webapp_root, converted_dir)
