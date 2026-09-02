"""合成敏感语料：覆盖每条规则的正例与反例，顺序固定，供 golden 比对与单测使用。

本文件会被发布闸（test_no_secrets.py）扫描，所以所有正例夹具都在运行时拼接，
源码里不出现任何完整的敏感形状字面量。词表与本机身份词一律用假值：
words 用 zzqx / plumbus / wibbleflange，identity_words 用 bob / alice。
"""

from __future__ import annotations

#: 传给 scan 的假词表词（子串语义）。
WORDS = ["zzqx", "plumbus", "wibbleflange"]

#: 传给 scan 的假本机身份词（词边界语义）。
IDENTITY_WORDS = ["bob", "alice"]

_HEX16 = "0123456789abcdef"


def _hex(n: int) -> str:
    """运行期生成 n 位十六进制串，避免源码里出现完整 hash 字面量。"""
    return (_HEX16 * ((n + 15) // 16))[:n]


def _local_home(name: str) -> str:
    return "C:\\Users\\" + name + "\\repo"


def _unc_home(name: str) -> str:
    return "\\\\host\\C$\\Users\\" + name + "\\repo"


def _posix_home(name: str) -> str:
    return "/home/" + name + "/repo"


def _users_home(name: str) -> str:
    return "/Users/" + name + "/repo"


def _wiki(inner: str) -> str:
    """拼 wiki 链接：拆成单字符拼接，避免源码里出现 [[...]] 形状被发布闸误扫。"""
    return "[" + "[" + inner + "]" + "]"


def corpus_lines() -> list[str]:
    lines = []

    # --- HOME_DIR：三种形状，白名单内与白名单外各若干 ---
    lines.append(_local_home("mallory"))              # 正例：两版都命中
    lines.append(_unc_home("trent"))                  # 正例（UNC）
    lines.append(_posix_home("victor"))               # 正例（POSIX）
    lines.append(_users_home("zed"))                  # 正例（/Users/）
    lines.append(_local_home("example"))              # D8-1：旧命中，新白名单放行
    lines.append(_local_home("xxx"))                  # D8-1：同上
    lines.append(_unc_home("dev"))                    # D8-1：同上
    lines.append(_posix_home("carol"))                # D8-1：同上
    lines.append(_users_home("dave"))                 # D8-1：同上
    lines.append(_local_home("user"))                 # 反例：旧白名单
    lines.append(_unc_home("me"))                     # 反例：旧白名单
    lines.append(_posix_home("you"))                  # 反例：旧白名单
    lines.append(_users_home("someone"))              # 反例：旧白名单
    lines.append(r"C:\Users\.\repo")                  # D8-2：退化匹配单个点
    lines.append(r"C:\Users\..\repo")                 # D8-2：退化匹配两个点
    lines.append(r"C:\Users\1234\repo")               # D8-2：纯数字退化
    lines.append(_local_home("mallory.v2"))           # 正例：带点的正常用户名

    # --- KEIL_USER_FILE：白名单内外 ---
    lines.append("P.uvguix." + "mallory")             # 正例
    lines.append("P.uvguix." + "trent")               # 正例
    lines.append("P.uvguix.dev")                      # 反例：KEIL_USER_OK
    lines.append("P.uvguix.sample")                   # 反例：KEIL_USER_OK
    lines.append("P.uvguix.user")                     # 反例：KEIL_USER_OK

    # --- EMAIL / EMAIL_OK ---
    lines.append("mallory@" + "projx.com")            # 正例：非白名单域
    lines.append("trent@" + "fakemail.net")           # 正例：非白名单域
    lines.append("test@" + "example.invalid")         # D8-3：.invalid 顶级
    lines.append("git@" + "github.com")               # D8-3：git 官方地址
    lines.append("noreply@" + "github.com")           # D8-3：noreply 地址
    lines.append("dev@" + "users.noreply.github.com") # 反例：旧白名单
    lines.append("someone@" + "example.org")          # 反例：example.*
    lines.append("person@" + "example.com")           # 反例：example.*
    lines.append("local@" + "localhost")              # 反例：localhost
    lines.append("x@" + "example.net")                # 反例：example.*

    # --- SID ---
    lines.append("S-1-5-21-" + "1234567890-1234567890-1234567890-1001")  # 正例
    lines.append("S-1-5-" + "1001-1002-1003")                            # 正例
    lines.append("S-1-5-" + "1001")                                      # 反例：组数不够

    # --- FULL_HASH：40 与 64 位 ---
    lines.append(_hex(40))                            # 正例：40 位
    lines.append(_hex(64))                            # 正例：64 位
    lines.append(_hex(39))                            # 反例：39 位
    lines.append("g" * 40)                            # 反例：非十六进制
    lines.append(_hex(40) + "extra")                  # 反例：接字母破坏词边界

    # --- WIKI_LINK ---
    lines.append(_wiki("note-name"))              # 正例
    lines.append(_wiki("private-vault-note"))     # 正例
    lines.append(_wiki(":space:"))                # 正例：POSIX 字符类误报
    lines.append(_wiki('"a","b"'))                # 正例：嵌套 JSON 误报
    lines.append("[[6]]")                         # 反例：无字母，引用标记
    lines.append(_wiki("link"))                   # 正例
    lines.append(_wiki("wiki-link"))              # 正例

    # --- PRIVATE_KEY ---
    lines.append("-----BEGIN " + "PRIVATE KEY-----")          # 正例
    lines.append("-----BEGIN " + "RSA PRIVATE KEY-----")      # 正例
    lines.append("-----BEGIN PUBLIC KEY-----")                # 反例：非私钥

    # --- CREDENTIAL_URL ---
    lines.append("https://" + "mallory:secret@" + "example.com/repo")  # 正例
    lines.append("https://example.com/repo")              # 反例：无 userinfo
    lines.append("https://" + "mallory@" + "example.com") # 反例：无口令

    # --- TOKEN ---
    lines.append("ghp_" + "K7x9mQ2vLp4nR8sT6uW3yA1zB")    # 正例
    lines.append("sk-" + "example" + "C9fT2qX8mL4vR7sN1pW5tY3uB")  # D8-4：占位 token 放行
    lines.append("ghp_" + "abcdefghijklmnopqrstuvwxyz0123")        # 字母表顺序假 token（两版仍命中）
    lines.append("github_pat_" + "11AA22bb33CC44dd55EE66ff77GG88") # 正例
    lines.append("AKIA" + "ABCDEFGHIJKLMNOP")             # 正例
    lines.append("xoxb-" + "1234567890-1234567890-123456789")     # 正例
    lines.append("AIza" + "SyD8pL2vR7xT9qW3mN5cK4jH6fB1uZ0aE2g")  # 正例

    # --- SECRET_ASSIGNMENT / is_placeholder_secret 入口 ---
    lines.append("password=" + "real-secret-value")       # 正例
    lines.append("api_key=" + "notso-secret-12")          # 正例
    lines.append("password='" + "real-secret-77" + "'")   # 正例：带引号
    lines.append("password=<password>")                   # 反例：<> 占位
    lines.append("api_key=example-key")                   # 反例：example 占位
    lines.append("password=${secret}")                    # 反例：${} 占位
    lines.append("auth_token=changeme12345")              # 反例：changeme 占位
    lines.append("access_token=redacted-value")           # 反例：redacted 占位
    lines.append("client_secret=dummy12345")              # 反例：dummy 占位
    lines.append("password=" + "{{secret}}")              # 反例：{{}} 占位
    lines.append("api_key=" + "xxx1234567890")            # 反例：xxx 占位
    lines.append("password=***")                          # 反例：值过短

    # --- WORD（子串语义）---
    lines.append("the zzqx config value")                 # 正例：子串命中
    lines.append("zzqx")                                  # 正例
    lines.append("azzqxb")                                # 正例：嵌在别的词里仍是命中
    lines.append("plumbus is a made up word")             # 正例
    lines.append("wibbleflange")                          # 正例
    lines.append("nothing to see here")                   # 反例
    lines.append("zz")                                    # 反例：太短

    # --- IDENTITY（词边界语义）---
    lines.append("bob")                                   # 正例：行首行尾
    lines.append("alice")                                 # 正例
    lines.append("bobsled")                               # 反例：后接字母
    lines.append("mobob")                                 # 反例：前接字母
    lines.append("bob2")                                  # 反例：后接数字
    lines.append("2bob")                                  # 反例：前接数字
    lines.append("bob,")                                  # 正例：后接标点
    lines.append("(bob)")                                 # 正例：前后标点
    lines.append("BOB")                                   # 正例：大小写
    lines.append("alIce")                                 # 正例：混合大小写
    lines.append("x_bob_x")                               # 正例：下划线不算词字符
    lines.append("bob@example.com")                       # 正例：邮箱里也命中（EMAIL 被白名单放行）

    # --- 多规则同线 / 无害行 ---
    lines.append("moved " + "/home/" + "victor" + " to " + "C:\\Users\\" + "mallory" + "\\repo")
    lines.append("a perfectly ordinary line of project text")   # 全反例
    lines.append('version = "1.2.3"')                     # 全反例
    lines.append("# just a comment, nothing sensitive")   # 全反例

    # --- 本轮修复新增（A / F）---
    lines.append("support@" + "github.com")                       # A：整域白名单 → 修复后拦
    lines.append("mallory@" + "github.com.evil.com")              # A：后缀绕过（D8-3 放宽引入）
    lines.append("person@" + "example.com.evil.com")              # A：后缀绕过（抽取前就有）
    lines.append("git@" + "github.com")                           # A：白名单地址，修复前后都放行
    lines.append("noreply@" + "github.com")                       # A：白名单地址，修复前后都放行
    lines.append("dev@" + "users.noreply.github.com")             # A：完整域相等，修复前后都放行
    lines.append("x@" + "myexample.com")                          # A：fullmatch 不被前缀骗过，修复前后都拦
    lines.append(_wiki(":alpha:"))                                # F：POSIX 字符类，修复后放行
    lines.append(_wiki("a:b"))                                    # F：含冒号但不是字符类形状，修复前后都命中
    lines.append(_wiki(":not a class:"))                          # F：冒号包着但含空格，不是字符类，修复前后都命中

    return lines

#: 语料全量命中签名：与 golden 对拍同一个键 (line_index, rule, column, hit_length)，
#: 由本轮实际运行结果生成、逐条与旧侧 golden 对账后写死（仅 (98, EMAIL) 为修复项 A 新增）。
EXPECTED_HITS = [
    (1, 'HOME_DIR', 0, 16),
    (2, 'HOME_DIR', 0, 21),
    (3, 'HOME_DIR', 0, 12),
    (4, 'HOME_DIR', 0, 10),
    (17, 'HOME_DIR', 0, 19),
    (18, 'KEIL_USER_FILE', 1, 15),
    (19, 'KEIL_USER_FILE', 1, 13),
    (23, 'EMAIL', 0, 17),
    (24, 'EMAIL', 0, 18),
    (33, 'SID', 0, 46),
    (34, 'SID', 0, 20),
    (36, 'FULL_HASH', 0, 40),
    (37, 'FULL_HASH', 0, 64),
    (41, 'WIKI_LINK', 0, 13),
    (42, 'WIKI_LINK', 0, 22),
    (44, 'WIKI_LINK', 0, 11),
    (46, 'WIKI_LINK', 0, 8),
    (47, 'WIKI_LINK', 0, 13),
    (48, 'PRIVATE_KEY', 0, 27),
    (49, 'PRIVATE_KEY', 0, 31),
    (51, 'CREDENTIAL_URL', 0, 23),
    (54, 'TOKEN', 0, 29),
    (56, 'TOKEN', 0, 34),
    (57, 'TOKEN', 0, 41),
    (58, 'TOKEN', 0, 20),
    (59, 'TOKEN', 0, 36),
    (60, 'TOKEN', 0, 39),
    (61, 'SECRET_ASSIGNMENT', 0, 26),
    (62, 'SECRET_ASSIGNMENT', 0, 23),
    (63, 'SECRET_ASSIGNMENT', 0, 24),
    (73, 'WORD', 4, 4),
    (74, 'WORD', 0, 4),
    (75, 'WORD', 1, 4),
    (76, 'WORD', 0, 7),
    (77, 'WORD', 0, 12),
    (80, 'IDENTITY', 0, 3),
    (81, 'IDENTITY', 0, 5),
    (86, 'IDENTITY', 0, 3),
    (87, 'IDENTITY', 1, 3),
    (88, 'IDENTITY', 0, 3),
    (89, 'IDENTITY', 0, 5),
    (90, 'IDENTITY', 2, 3),
    (91, 'IDENTITY', 0, 3),
    (92, 'HOME_DIR', 6, 12),
    (92, 'HOME_DIR', 22, 16),
    (96, 'EMAIL', 0, 18),
    (97, 'EMAIL', 0, 27),
    (98, 'EMAIL', 0, 27),
    (102, 'EMAIL', 0, 15),
    (104, 'WIKI_LINK', 0, 7),
    (105, 'WIKI_LINK', 0, 17),
]

#: 无害行（扫描应为空命中）的行号，1-based。
NEGATIVE_LINE_INDICES = (93, 94, 95)
