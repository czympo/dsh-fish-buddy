"""鱼搭子 ↔ AstrBot 桥（挂件侧）

方向：电脑主动出站连服务器 `ws://<ip>:8777/ws`，不开任何入站口、不动路由器。

它只做四件事，全在后台线程里，界面上一行都看不到（出问题只写右键日志）：
    speak       → 塞进主窗的「待显示」队列（和插件递回话走同一条路：普通模式冒气泡、
                  游戏模式走弹幕），可选再用系统 TTS 念出来
    screenshot  → 复用主窗现成的 _拍一张给插件()（1280 宽、q75 JPEG）
    game_state  → 前台窗口标题 + 进程名 + 游戏模式开没开
    心跳/重连   → 断了就指数退避静默重连（1→2→4…30 秒封顶）

明确不做：键鼠控制、远程执行命令、读文件系统。收到的 speak **只显示/播报，不回灌模型**。

⚠ 依赖：`websockets`（`pip install websockets`）。没装的话桥直接不启动，
   挂件其它功能一切照旧 —— 只在右键日志里留一行。

接入（ball.py 里加 6 行，见同目录「接入说明.md」）：

    self.桥 = None
    try:
        import 挂件桥
        self.桥 = 挂件桥.挂件桥(self, 配置, 记右键)
        self.桥.启动()
    except Exception as e:
        记右键('桥没起来：%s' % e)
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

服务端默认 = 'ws://43.131.225.58:8777/ws'
版本 = '2026-11-06.2'

# 这些键可以直接写在 config.json 里（设置页不会动它们）；
# 也可以在 %LOCALAPPDATA%\鱼搭子\桥.json 里单独放一份，键名一样，config.json 优先。
默认 = {
    '桥开': True,
    '桥地址': 服务端默认,
    '桥口令': '',
    '桥语音播报': False,   # 真·念出声
    '桥语音语速': 0,        # -10～10，0 是正常
    '桥音色': '',           # 留空 = 自动挑（优先自然语音，如 Xiaoxiao）；也可填 "Xiaoxiao" / "Kangkang"
}


# ---------------------------------------------------------------- 前台窗口

def _前台窗口标题():
    """取前台窗口标题。注意 64 位下 HWND 不能当 int 收，必须先声明签名。"""
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetForegroundWindow.argtypes = []
        h = u.GetForegroundWindow()
        if not h:
            return ''
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        n = u.GetWindowTextLengthW(h)
        if n <= 0:
            return ''
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ''


def _前台进程名():
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.GetForegroundWindow.restype = wintypes.HWND
        h = u.GetForegroundWindow()
        if not h:
            return ''
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if not pid.value:
            return ''
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        ph = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not ph:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            k.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
            ]
            k.QueryFullProcessImageNameW.restype = wintypes.BOOL
            if k.QueryFullProcessImageNameW(ph, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            try:
                k.CloseHandle(ph)
            except Exception:
                pass
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------- TTS
#
# 用 Edge 的在线神经语音（edge-tts）：zh-CN-XiaoxiaoNeural / YunxiNeural 这一套，
# 比 Windows 自带的系统语音好听一个档次。
#
# 为什么不用系统语音：
#   · SAPI5（Huihui Desktop）是 2009 年的老库，机器味很重
#   · Windows「自然语音」得自己进设置里装，这台机器上没装
#   · 手写 Edge 的 WebSocket 协议也不行：微软的反滥用校验会 403
#     （Origin / UA / Sec-MS-GEC 都带对了照样 403），只有持续维护的库能跟上
#
# 所以：把 edge-tts 打进 exe（PyInstaller 的 hiddenimports 里加 edge_tts），
# 合成在本机做（走家庭网络，比云服务器 IP 更不容易被限），
# 播放用 winmm 的 MCI（纯 ctypes，不再引播放依赖）。

_TTS_锁 = threading.Lock()   # 串行播放，免得多句叠在一起
_默认音色 = 'zh-CN-XiaoxiaoNeural'


def _音色全名(名):
    """允许填简写：Xiaoxiao → zh-CN-XiaoxiaoNeural；填全名就原样用。"""
    名 = str(名 or '').strip()
    if not 名:
        return _默认音色
    if '-' in 名:
        return 名
    return 'zh-CN-%sNeural' % 名


def _播放mp3(路径):
    """用 winmm 的 MCI 播 mp3，同步等到放完。纯 ctypes。"""
    try:
        import ctypes
        from ctypes import wintypes

        mci = ctypes.WinDLL('winmm')
        buf = ctypes.create_unicode_buffer(512)
        mci.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HWND]
        mci.mciSendStringW.restype = wintypes.DWORD

        def 命令(文):
            return mci.mciSendStringW(文, buf, 512, None)

        alias = 'fishbridge'
        命令('close %s' % alias)
        码 = 命令('open "%s" type mpegvideo alias %s' % (路径, alias))
        if 码 != 0:
            # 某些机器上 mpegvideo 类型名不对，退回让系统自己认
            码 = 命令('open "%s" alias %s' % (路径, alias))
        if 码 != 0:
            return False
        命令('play %s wait' % alias)
        命令('close %s' % alias)
        return True
    except Exception:
        return False


async def _合成edge(文, 音色, 语速):
    """合成一段 mp3，返回 (字节, 实际用的音色名)。"""
    import edge_tts

    # edge-tts 的 rate 是 "+20%" 这种写法；语速 -10～10 映射到 -50%～+50%
    百分比 = max(-50, min(50, int(语速 or 0) * 3))
    速 = '%+d%%' % 百分比
    声 = _音色全名(音色)
    缓冲 = bytearray()
    async for 块 in edge_tts.Communicate(文, 声, rate=速).stream():
        if 块.get('type') == 'audio' and 块.get('data'):
            缓冲.extend(块['data'])
    return bytes(缓冲), 声


def _念出声(文, 语速=0, 音色=''):
    """念一句：本地合成 → 临时 mp3 → 播完删掉。整个播放串行（加锁）。"""
    文 = str(文 or '').strip()
    if not 文:
        return False
    with _TTS_锁:
        try:
            import asyncio
            import tempfile

            try:
                import edge_tts  # noqa: F401
            except Exception:
                return False

            音频, _声 = asyncio.run(_合成edge(文, 音色, 语速))
            if len(音频) < 2000:
                return False
            路径 = os.path.join(tempfile.gettempdir(), 'fish-tts-%d.mp3' % int(time.time() * 1000))
            with open(路径, 'wb') as f:
                f.write(音频)
            try:
                return _播放mp3(路径)
            finally:
                try:
                    os.remove(路径)
                except Exception:
                    pass
        except Exception:
            return False

# ---------------------------------------------------------------- 桥

class 挂件桥:
    def __init__(self, 主窗, 配置=None, 记=None):
        self.主窗 = 主窗
        self.配置源 = 配置 if isinstance(配置, dict) else {}
        self._记 = 记 if callable(记) else (lambda s: None)
        self.想停 = threading.Event()
        self.线程 = None
        self.连上过 = False
        self.最近错误 = ''
        self.收到播报 = 0
        self.收到命令 = 0
        self._当前连接 = None
        self._loop = None

    @property
    def 在线(self):
        """当前是否连着服务端。

        ⚠ websockets 12+ 的连接对象没有 `.closed`，只有 `.state`（枚举，OPEN/CLOSED）；
        老版本反过来只有 `.closed`。这里两种都认，不然一取属性就 AttributeError，
        被 getattr 默认值吃成 False —— 表现就是「明明连上了却永远说不在线」。
        """
        连 = self._当前连接
        if 连 is None:
            return False
        状态 = getattr(连, 'state', None)
        if 状态 is not None:
            return str(状态).upper().endswith('OPEN')
        return not getattr(连, 'closed', False)

    # -- 配置 --------------------------------------------------------
    def _读(self, 键, 默认值=None):
        值 = self.配置源.get(键)
        if 值 not in (None, ''):
            return 值
        # 退路：%LOCALAPPDATA%\鱼搭子\桥.json
        try:
            import paths
            路 = os.path.join(paths.用户目录(), '桥.json')
            if os.path.exists(路):
                with open(路, 'r', encoding='utf-8') as f:
                    另 = json.load(f) or {}
                if 键 in 另 and 另[键] not in (None, ''):
                    return 另[键]
        except Exception:
            pass
        return 默认值 if 默认值 is not None else 默认.get(键)

    def _地址(self):
        a = str(self._读('桥地址', 服务端默认) or 服务端默认).strip()
        if not a.startswith(('ws://', 'wss://')):
            a = 'ws://' + a
        if not a.rstrip('/').endswith('/ws'):
            a = a.rstrip('/') + '/ws'
        return a

    # -- 起停 --------------------------------------------------------
    def 启动(self):
        if not bool(self._读('桥开', True)):
            self._记('桥：配置里关着，不起')
            return False
        if not str(self._读('桥口令', '') or '').strip():
            self._记('桥：没填口令（桥口令），不起')
            return False
        try:
            import websockets  # noqa: F401
        except Exception:
            self._记('桥：没装 websockets，不起（pip install websockets）')
            return False
        if self.线程 is not None:
            return True
        self.线程 = threading.Thread(target=self._跑, name='fish-bridge', daemon=True)
        self.线程.start()
        self._记('桥：模块版本 %s' % 版本)
        self._记('桥：开始连 %s' % self._地址())
        return True

    def 停(self):
        self.想停.set()

    def 状态(self):
        return {
            '连上': self.连上过,
            '地址': self._地址(),
            '播报': self.收到播报,
            '命令': self.收到命令,
            '最近错误': self.最近错误,
        }

    # -- 主循环 ------------------------------------------------------
    def _跑(self):
        try:
            import asyncio

            asyncio.run(self._主循环())
        except Exception as e:
            self._记('桥：线程挂了（%s）' % e)

    async def _主循环(self):
        import asyncio

        import websockets

        self._loop = asyncio.get_running_loop()
        退 = 1.0
        while not self.想停.is_set():
            try:
                # ping_interval=None：心跳由服务端发（它每 30 秒 ping 一次），
                # websockets 库会自动回 pong，我们不用自己写。
                async with websockets.connect(
                    self._地址(), ping_interval=None, max_size=16 * 1024 * 1024, open_timeout=10
                ) as ws:
                    await ws.send(json.dumps(
                        {'type': 'hello', 'token': str(self._读('桥口令', '') or '')},
                        ensure_ascii=False,
                    ))
                    self.连上过 = True
                    self.最近错误 = ''
                    退 = 1.0
                    self._当前连接 = ws
                    self._记('桥：连上了')
                    async for 原 in ws:
                        try:
                            await self._收(json.loads(原))
                        except Exception as e:
                            self._记('桥：这条收不了（%s）' % e)
                    self._当前连接 = None
            except Exception as e:
                self.最近错误 = '%s' % e
                # ⚠ 静默：只写日志，绝不往界面上弹
                self._记('桥：断了（%s），%.0f 秒后重连' % (e, 退))
            self.连上过 = False
            if self.想停.is_set():
                break
            await asyncio.sleep(退)
            退 = min(退 * 2.0, 30.0)

    # -- 收包 --------------------------------------------------------
    async def _收(self, 包):
        t = 包.get('type')
        if t == 'welcome':
            self._记('桥：服务端问好（%s）' % (包.get('server') or '?'))
            return
        if t == 'speak':
            文 = str(包.get('text') or '').strip()
            if not 文:
                return
            self.收到播报 += 1
            self._显示(文)
            return
        if t == 'cmd':
            self.收到命令 += 1
            命令 = str(包.get('cmd') or '')
            号 = 包.get('id')
            # 截图/取状态是同步的（几百毫秒），丢到线程池里跑，别堵住接收循环
            try:
                import asyncio

                结果 = await asyncio.get_running_loop().run_in_executor(None, self._干活, 命令)
            except Exception as e:
                结果 = {'ok': False, 'msg': '干不了：%s' % e}
            结果['type'] = 'result'
            结果['id'] = 号
            await self._发(结果)
            return
        # 其它一律忽略（心跳、未知类型）

    def 说(self, 文本):
        """把一句话送给服务端（当成主人说的）。True = 已经排进发送队列。"""
        文 = str(文本 or '').strip()
        if not 文 or not self.在线 or self._loop is None:
            return False
        try:
            import asyncio

            asyncio.run_coroutine_threadsafe(self._发voice(文), self._loop)
            return True
        except Exception:
            return False

    async def _发voice(self, 文):
        try:
            等 = getattr(self, '_当前连接', None)
            if 等 is None or not self.在线:
                return
            await 等.send(json.dumps({'type': 'voice', 'text': 文}, ensure_ascii=False))
        except Exception as e:
            self.最近错误 = '送话失败：%s' % e

    async def _发(self, 包):
        try:
            等 = getattr(self, '_当前连接', None)
            if 等 is None:
                return
            await 等.send(json.dumps(包, ensure_ascii=False))
        except Exception as e:
            self.最近错误 = '回包失败：%s' % e

    # -- 干活 --------------------------------------------------------
    def _干活(self, 命令):
        if 命令 == 'screenshot':
            return self._截图()
        if 命令 == 'game_state':
            return self._游戏状态()
        return {'ok': False, 'msg': '不认识的命令：%s' % (命令 or '空')}

    def _截图(self):
        """复用主窗现成的「拍一张给插件」：它已经缩到 1280 宽、压成 q75 JPEG。"""
        拍 = getattr(self.主窗, '_拍一张给插件', None)
        if not callable(拍):
            return {'ok': False, 'msg': '这个版本的挂件还不能截图'}
        try:
            结果 = 拍()
        except Exception as e:
            return {'ok': False, 'msg': '拍不了：%s' % e}
        if not 结果:
            return {'ok': False, 'msg': '这一下没抓到画面'}
        try:
            字节, 尺寸 = 结果
            return {
                'ok': True,
                'data': {
                    'image': base64.b64encode(字节).decode('ascii'),
                    'size': '%dx%d' % (尺寸[0], 尺寸[1]),
                    'title': _前台窗口标题(),
                },
            }
        except Exception as e:
            return {'ok': False, 'msg': '收拾不了这张图：%s' % e}

    def _游戏状态(self):
        return {
            'ok': True,
            'data': {
                '前台': _前台进程名() or '未知',
                '窗口': _前台窗口标题(),
                '游戏': bool(getattr(self.主窗, '游戏模式', False)),
            },
        }

    # -- 播报 --------------------------------------------------------
    def _显示(self, 文):
        """塞进主窗的待显示队列 —— 跟插件递回话是同一条路，气泡/弹幕都归它管。"""
        try:
            q = getattr(self.主窗, '待显示', None)
            if q is not None:
                q.put(('回复', 文))
        except Exception as e:
            self._记('桥：显示不了（%s）' % e)
        if bool(self._读('桥语音播报', False)):
            try:
                threading.Thread(
                    target=_念出声,
                    args=(文, self._读('桥语音语速', 0) or 0, str(self._读('桥音色', '') or '')),
                    name='fish-bridge-tts',
                    daemon=True,
                ).start()
            except Exception:
                pass
