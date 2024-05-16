import inspect
import os
import json
import threading
import traceback
from collections import deque

import requests
import time

import asyncio
import aiohttp
from PyQt5.QtCore import pyqtSignal, QObject

from ..common.config import cfg, Language
from ..common.logger import logger
from ..common.signals import signalBus
from ..common.util import getPortTokenServerByPid
from .exceptions import *

requests.packages.urllib3.disable_warnings()

TAG = "Connector"


class PastRequest:
    def __init__(self, func: str, params_dict, kwargs):
        self.func = func  # 此处需要func.__name__ 而不是func本身的obj
        self.params_dict = params_dict
        self.kwargs = kwargs
        self.response = None
        self.timestamp = time.time()

    def __str__(self):
        # 如果是None的成员, 不会被打印; 没打印response就是没有响应;
        attrs = [f"{k}={v!r}" for k, v in self.__dict__.items()
                 if v is not None]
        return f"PastRequest({', '.join(attrs)})"


def needLcu():
    def decorator(func):
        async def wrapper(*args, **kwargs):
            if connector.lcuSess is None:
                raise ReferenceError

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def retry(count=5, retry_sep=0):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            logger.info(f"call %s" % func.__name__, TAG)

            # 获取函数的参数信息
            func_params = inspect.signature(func).parameters
            param_names = list(func_params.keys())

            tmp_args = args
            if param_names[0] == "self":
                # args[0] 是 self(connector) 的实例, 兼容静态方法
                param_names = param_names[1:]
                tmp_args = args[1:]

            # 构建参数字典，将参数名与对应的实参值一一对应
            params_dict = {param: arg for param,
                           arg in zip(param_names, tmp_args)}

            logger.debug(f"args = {params_dict}|kwargs = {kwargs}", TAG)
            # logger.debug(f"args = {args[1:]}|kwargs = {kwargs}", TAG)

            with connector.dqLock:
                req_obj = PastRequest(
                    func.__name__,
                    params_dict,
                    kwargs
                )
                connector.callStack.append(req_obj)

            exce = None
            for _ in range(count):
                try:
                    async with connector.semaphore:
                        res = await func(*args, **kwargs)
                except BaseException as e:
                    time.sleep(retry_sep)
                    exce = e

                    if isinstance(e, SummonerNotFound):  # SummonerNotFound 再重试会报 429 (限流)
                        raise e
                    continue
                else:
                    break
            else:
                # 有异常抛异常, 没异常抛 RetryMaximumAttempts
                exce = exce if exce else RetryMaximumAttempts(
                    "Exceeded maximum retry attempts.")

                # ReferenceError 为 LCU 未就绪仍有请求发送时抛出, 直接吞掉不用提示
                # 其余异常弹一个提示
                if type(exce) is not ReferenceError:
                    signalBus.lcuApiExceptionRaised.emit(
                        func.__name__, exce)

                with connector.dqLock:
                    req_obj.response = exce

                logger.exception(f"exit {func.__name__}", exce, TAG)

                raise exce

            with connector.dqLock:
                req_obj.response = res

            logger.info(f"exit {func.__name__}", TAG)
            logger.debug(f"result = {res}", TAG)

            return res

        return wrapper

    return decorator


class LcuWebSocket():
    def __init__(self, port, token):
        self.port = port
        self.token = token

        self.events = []
        self.subscribes = []

    def subscribe(self, event: str, uri: str = '', type: tuple = ('Update', 'Create', 'Delete')):
        def wrapper(func):
            self.events.append(event)
            self.subscribes.append({
                'uri': uri,
                'type': type,
                'callable': func
            })
            return func

        return wrapper

    def matchUri(self, data):
        for s in self.subscribes:
            # If the 'uri' or 'type' is empty, it matches any event.
            if not (s.get('uri') or s.get('type')) or (
                    data.get('uri') == s['uri'] and data.get('eventType') in s['type']):
                logger.info(s['uri'], TAG)
                logger.debug(data, TAG)
                asyncio.create_task(s['callable'](data))
                # return

    async def runWs(self):
        # 创建WebSocket连接
        self.session = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth('riot', self.token),
            headers={
                'Content-type': 'application/json',
                'Accept': 'application/json'
            }
        )
        address = f'wss://127.0.0.1:{self.port}/'
        self.ws = await self.session.ws_connect(address, ssl=False)

        for event in self.events:
            # [5, event] 向客户端订阅事件
            await self.ws.send_json([5, event])

        while True:
            msg = await self.ws.receive()

            if msg.type == aiohttp.WSMsgType.TEXT and msg.data != '':
                data = json.loads(msg.data)[2]
                self.matchUri(data)
                continue

            if msg.type == aiohttp.WSMsgType.CLOSED:
                logger.info("WebSocket closed", TAG)
                break

        await self.session.close()

    async def start(self):
        if "OnJsonApiEvent" in self.events:
            raise AssertionError(
                "You should not use OnJsonApiEvent to subscribe to all events. If you wish to debug "
                "the program, comment out this line.")
        # 创建一个任务来运行WebSocket
        self.task = asyncio.create_task(self.runWs())

    async def close(self):
        self.task.cancel()
        await self.session.close()


class LolClientConnector(QObject):

    def __init__(self):
        super().__init__()
        self.semaphore = None
        self.lcuSess = None
        self.sgpSess = None
        self.port = None
        self.token = None
        self.server = None
        self.inMainLand = False

        self.manager = None
        self.maxRefCnt = cfg.get(cfg.apiConcurrencyNumber)

        self.dqLock = threading.Lock()
        self.callStack = deque(maxlen=10)

    async def start(self, pid):
        self.pid = pid

        self.port, self.token, self.server = getPortTokenServerByPid(pid)
        
        self.semaphore = asyncio.Semaphore(self.maxRefCnt)

        # 初始化与客户端的通信
        self.__initSessions()

        # 判断是否在大陆
        self.__initPlatformInfo()

        await self.__initManager()

        # 创建 game 等文件夹
        self.__initFolder()

        await self.__runListener()

        logger.critical(f"connector started, server: {self.server}", TAG)

    async def __runListener(self):
        self.listener = LcuWebSocket(self.port, self.token)

        # 当召唤师当前的个人信息发生变化时触发
        @self.listener.subscribe(event='OnJsonApiEvent_lol-summoner_v1_current-summoner',
                                 uri='/lol-summoner/v1/current-summoner',
                                 type=('Update',))
        async def onCurrentSummonerProfileChanged(event):
            signalBus.currentSummonerProfileChanged.emit(event['data'])

        # 当游戏状态发生变化时触发
        @self.listener.subscribe(event='OnJsonApiEvent_lol-gameflow_v1_gameflow-phase',
                                 uri='/lol-gameflow/v1/gameflow-phase',
                                 type=('Update',))
        async def onGameFlowPhaseChanged(event):
            signalBus.gameStatusChanged.emit(event['data'])

        # 当英雄选择发生变化时触发
        @self.listener.subscribe(event='OnJsonApiEvent_lol-champ-select_v1_session',
                                 uri='/lol-champ-select/v1/session',
                                 type=('Update',))
        async def onChampSelectChanged(event):
            signalBus.champSelectChanged.emit(event)

        # TODO(@liangyu) DDEBUG
        # @self.listener.subscribe(event='OnJsonApiEvent', type=())
        # async def onDebugListen(event):
        #     print(event)

        await self.listener.start()

    async def close(self):
        try:
            await self.listener.close()
        except:
            pass

        await self.lcuSess.close()

        if self.sgpSess:
            await self.sgpSess.close()

        self.__init__()

    def __initSessions(self):
        self.lcuSess = aiohttp.ClientSession(
            base_url=f'https://127.0.0.1:{self.port}',
            auth=aiohttp.BasicAuth('riot', self.token)
        )

        if not self.server:
            return

        if self.server.lower() in ('hn1', 'hn10'):
            url = f'https://{self.server.lower()}-cloud-sgp.lol.qq.com:21019'
        else:
            url = f'https://{self.server.lower()}-sgp.lol.qq.com:21019'

        self.sgpSess = aiohttp.ClientSession(
            base_url=url
        )

    def __initFolder(self):
        if not os.path.exists("app/resource/game"):
            os.mkdir("app/resource/game")

        for folder in [
            "champion icons",
            "item icons",
            "profile icons",
            "rune icons",
            "summoner spell icons",
        ]:
            p = f"app/resource/game/{folder}"
            if not os.path.exists(p):
                os.mkdir(p)

    async def __initManager(self):
        items = await self.__json_retry_get("/lol-game-data/assets/v1/items.json")
        spells = await self.__json_retry_get(
            "/lol-game-data/assets/v1/summoner-spells.json")
        runes = await self.__json_retry_get("/lol-game-data/assets/v1/perks.json")
        queues = await self.__json_retry_get("/lol-game-queues/v1/queues")
        champions = await self.__json_retry_get(
            "/lol-game-data/assets/v1/champion-summary.json")
        skins = await self.__json_retry_get("/lol-game-data/assets/v1/skins.json")

        self.manager = JsonManager(
            items, spells, runes, queues, champions, skins)

    def __initPlatformInfo(self):
        if self.server:
            mainlandPlatforms = {'tj100', 'hn1', 'cq100',
                                 'gz100', 'nj100', 'bgp2', 'hn10', 'tj101'}

            self.inMainLand = self.server.lower() in mainlandPlatforms

    async def __json_retry_get(self, url, max_retries=5):
        """
        根据 httpStatus 字段值, retry 获取数据

        用于软件初始化阶段

        @param url:
        @param max_retries:
        @return: json
        @rtype: dict
        """
        retries = 0
        while retries < max_retries:
            try:
                result = await self.__get(url)
                result = await result.json()

            # 客户端刚打开, Service 正在初始化
            # 有部分请求可能会 ConnectionError, 直接忽略重试
            except aiohttp.ClientConnectorError:
                retries += 1
                time.sleep(.5)
                continue
            
            # TODO(@liangyu) 如果是 list 就直接返回？ 为什么？
            if type(result) is list:
                return result

            # 如果有才判定, 有部分相应成功时没有 httpStatus
            elif result.get("httpStatus") and result.get("httpStatus") != 200:
                time.sleep(.5)
                retries += 1
            else:
                return result

        # 最大重试次数, 抛异常
        raise RetryMaximumAttempts("Exceeded maximum retry attempts.")

    # 获取符文图标
    @retry()
    async def getRuneIcon(self, runeId):
        if runeId == 0:
            return "app/resource/images/rune-0.png"

        icon = f"app/resource/game/rune icons/{runeId}.png"
        if not os.path.exists(icon):
            path = self.manager.getRuneIconPath(runeId)
            res = await self.__get(path)

            with open(icon, "wb") as f:
                f.write(await res.read())

        return icon

    # 获取召唤师信息
    @retry()
    async def getCurrentSummoner(self):
        res = await self.__get("/lol-summoner/v1/current-summoner")
        res = await res.json()

        if not "summonerId" in res:
            raise Exception()

        return res

    @retry()
    async def getInstallFolder(self):
        res = await self.__get("/data-store/v1/install-dir")
        return await res.json()

    @retry()
    async def getProfileIcon(self, iconId):
        icon = f"./app/resource/game/profile icons/{iconId}.jpg"

        if not os.path.exists(icon):
            path = self.manager.getSummonerProfileIconPath(iconId)
            res = await self.__get(path)

            with open(icon, "wb") as f:
                f.write(await res.read())

        return icon

    @retry()
    async def getItemIcon(self, iconId):
        if iconId == 0:
            return "app/resource/images/item-0.png"

        icon = f"app/resource/game/item icons/{iconId}.png"

        if not os.path.exists(icon):
            path = self.manager.getItemIconPath(iconId)
            res = await self.__get(path)

            with open(icon, "wb") as f:
                f.write(await res.read())

        return icon

    @retry()
    async def getSummonerSpellIcon(self, spellId):
        icon = f"app/resource/game/summoner spell icons/{spellId}.png"

        if not os.path.exists(icon):
            path = self.manager.getSummonerSpellIconPath(spellId)
            res = await self.__get(path)

            with open(icon, "wb") as f:
                f.write(await res.read())

        return icon

    @retry()
    async def getChampionIcon(self, championId) -> str:
        """
        @param championId:
        @return: path
        @rtype: str
        """

        if championId in [-1, 0]:
            return "app/resource/images/champion-0.png"

        icon = f"app/resource/game/champion icons/{championId}.png"

        if not os.path.exists(icon):
            path = self.manager.getChampionIconPath(championId)
            res = await self.__get(path)

            with open(icon, "wb") as f:
                f.write(await res.read())

        return icon

    @retry()
    async def getSummonerByName(self, name):
        params = {"name": name}
        res = await self.__get(f"/lol-summoner/v1/summoners", params)
        res = await res.json()

        return res

    @retry()
    async def getSummonerByPuuid(self, puuid):
        res = await self.__get(f"/lol-summoner/v2/summoners/puuid/{puuid}")
        res = await res.json()

        if "errorCode" in res and res["httpStatus"] == 400:
            raise SummonerNotFound()

        return res

    @retry(5, 1)
    async def getSummonerGamesByPuuidSlowly(self, puuid, begIndex=0, endIndex=4):
        """
        Retrieves a list of summoner games by puuid using a slow and retry mechanism.

        Parameters:
            puuid (str): The puuid of the summoner.
            begIndex (int): The beginning index of the games to retrieve. Default is 0.
            endIndex (int): The ending index of the games to retrieve. Default is 4.

        Returns:
            list: A list of summoner games.

        Raises:
            SummonerGamesNotFound: If the summoner games are not found.
        """
        params = {"begIndex": begIndex, "endIndex": endIndex}
        res = await self.__get(
            f"/lol-match-history/v1/products/lol/{puuid}/matches", params
        )
        res = await res.json()

        if "games" not in res:
            raise SummonerGamesNotFound()

        return res["games"]

    @retry()
    async def getSummonerGamesByPuuid(self, puuid, begIndex=0, endIndex=4):
        """
        Retrieves a list of summoner games by PUUID.

        Args:
            puuid (str): The PUUID of the summoner.
            begIndex (int, optional): The starting index of the games to retrieve. Defaults to 0.
            endIndex (int, optional): The ending index of the games to retrieve. Defaults to 4.

        Returns:
            list: A list of summoner games.

        Raises:
            SummonerGamesNotFound: If the summoner games are not found.
        """
        params = {"begIndex": begIndex, "endIndex": endIndex}
        res = await self.__get(
            f"/lol-match-history/v1/products/lol/{puuid}/matches", params
        )
        res = await res.json()

        if "games" not in res:
            raise SummonerGamesNotFound()

        return res["games"]

    @retry()
    async def getGameDetailByGameId(self, gameId):
        res = await self.__get(f"/lol-match-history/v1/games/{gameId}")

        return await res.json()

    @retry()
    async def getRankedStatsByPuuid(self, puuid):
        res = await self.__get(f"/lol-ranked/v1/ranked-stats/{puuid}")

        res = await res.json()

        if "errorCode" in res:
            raise SummonerRankInfoNotFound()

        return res

    @retry()
    async def setProfileBackground(self, skinId):
        data = {
            "key": "backgroundSkinId",
            "value": skinId,
        }
        res = await self.__post(
            "/lol-summoner/v1/current-summoner/summoner-profile", data=data
        )

        return await res.json()

    @retry()
    async def setOnlineStatus(self, message):
        data = {"statusMessage": message}
        res = await self.__put("/lol-chat/v1/me", data=data)

        return await res.json()

    @retry()
    async def setTierShowed(self, queue, tier, division):
        data = {
            "lol": {
                "rankedLeagueQueue": queue,
                "rankedLeagueTier": tier,
                "rankedLeagueDivision": division,
            }
        }

        res = await self.__put("/lol-chat/v1/me", data=data)

        return await res.json()

    @retry()
    async def reconnect(self):
        return await self.__post("/lol-gameflow/v1/reconnect")

    @retry()
    async def removeTokens(self):
        reference = await self.__get("/lol-chat/v1/me")
        reference = await reference.json()

        banner = reference['lol'].get('bannerIdSelected')

        data = {
            "challengeIds": [],
            "bannerAccent": banner,
        }

        res = await self.__post(
            "/lol-challenges/v1/update-player-preferences/", data=data
        )

        return await res.read()

    @retry()
    async def removePrestigeCrest(self):
        ref = await self.__get('/lol-regalia/v2/current-summoner/regalia')
        ref = await ref.json()
        bannerType = ref.get("preferredBannerType")

        data = {
            "preferredCrestType": "prestige",
            "preferredBannerType": bannerType,
            'selectedPrestigeCrest': 22
        }

        res = await self.__put('/lol-regalia/v2/current-summoner/regalia', data=data)
        return await res.json()

    @retry()
    async def create5v5PracticeLobby(self, lobbyName, password):
        data = {
            "customGameLobby": {
                "configuration": {
                    "gameMode": "PRACTICETOOL",
                    "gameMutator": "",
                    "gameServerRegion": "",
                    "mapId": 11,
                    "mutators": {"id": 1},
                    "spectatorPolicy": "AllAllowed",
                    "teamSize": 5,
                },
                "lobbyName": lobbyName,
                "lobbyPassword": password,
            },
            "isCustom": True,
        }
        res = await self.__post("/lol-lobby/v2/lobby", data=data)
        return await res.json()

    @retry()
    async def setOnlineAvailability(self, availability):
        data = {"availability": availability}

        res = await self.__put("/lol-chat/v1/me", data=data)
        return res

    @retry()
    async def acceptMatchMaking(self):
        res = await self.__post("/lol-matchmaking/v1/ready-check/accept")
        return res

    @retry()
    async def getGameflowSession(self):
        # FIXME
        # 若刚进行完一场对局, 随后开启一盘自定义, 玩家在红色方且蓝色方没人时,
        # 该接口会返回上一局中蓝色方的队员信息 (teamOne or teamTwo)
        res = await self.__get("/lol-gameflow/v1/session")
        return await res.json()

    @retry()
    async def getChampSelectSession(self):
        res = await self.__get("/lol-champ-select/v1/session")
        return await res.json()

    # 同意交换
    @retry()
    async def acceptTrade(self, id):
        res = await self.__post(f"/lol-champ-select/v1/session/trades/{id}/accept")
        return await res.json()

    # 备战席交换
    async def benchSwap(self, championId):
        res = await self.__post(f"/lol-champ-select/v1/session/bench/swap/{championId}")
        return await res.json()

    # 获取当前选择英雄
    @retry()
    async def getCurrentChampion(self):
        res = await self.__get("/lol-champ-select/v1/current-champion")
        return await res.json()

    # 摇骰子
    @retry()
    async def reroll(self):
        res = await self.__post("/lol-champ-select/v1/session/my-selection/reroll")
        return await res.json()

    # 选择英雄
    @retry()
    async def selectChampion(self, actionsId, championId, completed=None):
        data = {
            "championId": championId,
            'type': 'pick',
        }

        if completed:
            data['completed'] = True

        res = await self.__patch(
            f"/lol-champ-select/v1/session/actions/{actionsId}", data=data)

        return await res.read()

    # 禁用英雄
    @retry()
    async def banChampion(self, actionsId, championId, completed=None):
        data = {
            "championId": championId,
            'type': 'ban',
        }

        if completed:
            data['completed'] = completed

        res = await self.__patch(
            f"/lol-champ-select/v1/session/actions/{actionsId}", data=data)

        return await res.read()

    # 获取皮肤轮盘
    @retry()
    async def getSkinCarousel(self):
        # res = await self.__get("/lol-champ-select/v1/pickable-skin-ids") //这个是所有可用的皮肤，不是自己可用的皮肤--!
        res = await self.__get("/lol-champ-select/v1/skin-carousel-skins")
        return await res.json()

    # 选皮肤、召唤师技能
    @retry()
    async def selectConfig(self, skinId, spell1Id=None, spell2Id=None, wardSkinId=None):
        data = {
            "selectedSkinId": skinId
        }

        # 4-点燃 12-闪现 14-传送 **推测未验证**
        if spell1Id:
            data["spell1Id"] = spell1Id
        if spell2Id:
            data["spell2Id"] = spell2Id
        if wardSkinId:
            # 不知道是什么，默认-1
            data["wardSkinId"] = wardSkinId

        res = await self.__patch("/lol-champ-select/v1/session/my-selection", data)
        return await res.json()

    @retry()
    async def getSummonerById(self, summonerId):
        res = await self.__get(f"/lol-summoner/v1/summoners/{summonerId}")

        return await res.json()

    # @retry()
    async def getGameStatus(self):
        res = await self.__get("/lol-gameflow/v1/gameflow-phase")
        res = await res.text()

        return res[1:-1]

    @retry()
    async def getMapSide(self):
        res = await self.__get("/lol-champ-select/v1/pin-drop-notification")
        res = await res.json()

        return res.get("mapSide", "")

    @retry()
    async def getReadyCheckStatus(self):
        res = await self.__get("/lol-matchmaking/v1/ready-check")

        return await res.json()

    async def spectate(self, summonerName):
        info = await self.getSummonerByName(summonerName)
        puuid = info.get('puuid')

        if not puuid:
            raise SummonerNotFound()

        data = {
            'allowObserveMode': 'ALL',
            'dropInSpectateGameId': summonerName,
            'gameQueueType': "",
            'puuid': puuid,
        }

        res = await self.__post(
            f"/lol-spectator/v1/spectate/launch", data=data)

        res = await res.read()

        if res != b'':
            raise SummonerNotInGame()

        return res

    def getConversations(self):
        res = self.__get("/lol-chat/v1/conversations").json()

        return res

    def getHelp(self):
        res = self.__get("/help").json()
        return res

    @retry()
    async def sendFriendRequest(self, name):
        summoner = self.getSummonerByName(name)
        summonerId = summoner['summonerId']

        data = {
            "name": name,
        }

        res = await self.__post('/lol-chat/v1/friend-requests', data=data)

        print(await res.read())

    def dodge(self):
        res = self.__post(
            '/lol-login/v1/session/invoke?destination=lcdsServiceProxy&method=call&args=["","teambuilder-draft","quitV2",""])').content

        return res

    @retry()
    def sendNotificationMsg(self, title, content):
        data = {
            "critical": True,
            "data": {
                "details": content,
                "title": title,
            },
            "detailKey": 'pre_translated_details',
            "dismissible": True,
            "id": 0,
            "state": 'toast',
            "titleKey": 'pre_translated_title',
            "type": 'ranked_summary',
        }

        res = self.__post(
            "/player-notifications/v1/notifications", data=data).json()

        return res

    @retry()
    async def playAgain(self):
        res = await self.__post("/lol-lobby/v2/play-again")

        return await res.read()

    @retry()
    async def getClientZoom(self):
        res = await self.__get("/riotclient/zoom-scale")

        return await res.json()

    async def getGameReplay(self, gameId):
        data = {"componentType": "replay-button_match-history", "gameId": gameId}
        res = await self.__post(f"/lol-replays/v1/rofls/{gameId}/download", data=data)

        return res

    async def getReplayMetadata(self, gameId):
        res = await self.__get(f"/lol-replays/v1/metadata/{gameId}")

        return await res.json()

    async def getReplayPath(self):
        res = await self.__get("/lol-replays/v1/rofls/path")

        return await res.json()

    @retry()
    async def getSGPtoken(self):
        res = await self.__get("/entitlements/v1/token")
        res = await res.json()

        return res['accessToken']
    
    async def restartClient(self):
        res = await self.__post("/riotclient/kill-and-restart-ux")

        return res

    async def getSummonerGamesByPuuidViaSGP(self, token, puuid, begIdx, endIdx):
        logger.debug(
            f"getSummonerGamesByPuuidViaSGP called, {token = }, {puuid = }", TAG)

        url = f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY"
        params = {
            'startIndex': begIdx,
            'count': endIdx - begIdx + 1,
        }

        res = await self.__sgp__get(url, token, params)
        return await res.json()

    async def getRankedStatsByPuuidViaSGP(self, token, puuid):
        logger.debug(
            f"getRankedStatsByPuuidViaSGP called, {token = }, {puuid = }", TAG)

        url = f'/leagues-ledge/v2/leagueLadders/puuid/{puuid}'
        res = await self.__sgp__get(url, token)

        return await res.json()

    async def getSummonerByPuuidViaSGP(self, token, puuid):
        """
        该接口的返回值与 `self.getSummonerByPuuid()` 相比，拿不到召唤师的 `tagLine`
        即数字编号信息
        """
        logger.debug(
            f"getSummonerByPuuidViaSGP called, {token = }, {puuid = }", TAG)

        url = f"/summoner-ledge/v1/regions/{self.server.lower()}/summoners/puuid/{puuid}"

        res = await self.__sgp__get(url, token)
        return await res.json()

    def isInMainland(self):
        return self.inMainLand

    @needLcu()
    async def __get(self, path, params=None):
        return await self.lcuSess.get(path, params=params, ssl=False)

    @needLcu()
    async def __post(self, path, data=None):
        headers = {"Content-type": "application/json"}
        return await self.lcuSess.post(path, json=data, headers=headers, ssl=False)

    @needLcu()
    async def __put(self, path, data=None):
        return await self.lcuSess.put(path, json=data, ssl=False)

    @needLcu()
    async def __patch(self, path, data=None):
        return await self.lcuSess.patch(path, json=data, ssl=False)

    async def __sgp__get(self, path, token, params=None):
        assert self.inMainLand

        headers = {
            "Authorization": f"Bearer {token}"
        }

        return await self.sgpSess.get(path, params=params, ssl=False, headers=headers)

    def getLoginSummonerByPid(self, pid):
        port, token, _ = getPortTokenServerByPid(pid)
        url = f'https://riot:{token}@127.0.0.1:{port}/lol-summoner/v1/current-summoner'
        return requests.get(url, verify=False).json()


class JsonManager:
    # 曾经奥恩可以升级的杰作装备
    masterpieceItemsMap = {
        7003: 6664,  # 涡轮炼金罐
        7004: 3068,  # 日炎圣盾
        7007: 6672,  # 海妖杀手
        7008: 6673,  # 不朽盾弓
        7022: 4005,  # 帝国指令
    }

    def __init__(self, itemData, spellData, runeData, queueData, champions, skins):
        self.items = {item["id"]: item["iconPath"] for item in itemData}
        self.spells = {item["id"]: item["iconPath"] for item in spellData[:-3]}
        self.runes = {item["id"]: item["iconPath"] for item in runeData}

        self.champs = {item["id"]: item["name"] for item in champions}

        self.champions = {item: {"skins": {}} for item in self.champs.values()}
        self.queues = {
            item["id"]: {"mapId": item["mapId"], "name": item["name"]}
            for item in queueData
        }

        for item in skins.values():
            championId = item["id"] // 1000
            self.champions[self.champs[championId]
                           ]["skins"][item["name"]] = item["id"]
            self.champions[self.champs[championId]]["id"] = championId

        for oldId, nowId in JsonManager.masterpieceItemsMap.items():
            self.items[oldId] = self.items[nowId]

    def getItemIconPath(self, iconId):
        if iconId != 0:
            try:
                return self.items[iconId]
            except:
                logger.error(f"getItemIconPath, iconId: {iconId}", tag=TAG)

        return "/lol-game-data/assets/ASSETS/Items/Icons2D/gp_ui_placeholder.png"

    def getSummonerSpellIconPath(self, spellId):
        if spellId != 0:
            return self.spells[spellId]
        else:
            return "/lol-game-data/assets/data/spells/icons2d/summoner_empty.png"

    def getRuneIconPath(self, runeId):
        return self.runes[runeId]

    def getSummonerProfileIconPath(self, iconId):
        return f"/lol-game-data/assets/v1/profile-icons/{iconId}.jpg"

    def getChampionIconPath(self, championId):
        return f"/lol-game-data/assets/v1/champion-icons/{championId}.png"

    def getMapNameById(self, mapId):
        maps = {
            -1: ("特殊地图", "Special map"),
            11: ("召唤师峡谷", "Summoner's Rift"),
            12: ("嚎哭深渊", "Howling Abyss"),
            21: ("极限闪击", "Nexus Blitz"),
            30: ("斗魂竞技场", "Arena"),
        }

        key = mapId if mapId in maps else -1
        index = 1 if cfg.language.value == Language.ENGLISH else 0

        return maps[key][index]

    def getNameMapByQueueId(self, queueId):
        if queueId == 0:
            return {
                "name": "Custom" if cfg.language.value == Language.ENGLISH else "自定义"
            }

        data = self.queues[queueId]
        mapId = data["mapId"]
        name = data["name"]

        if cfg.language.value == Language.ENGLISH:
            with open("app/resource/i18n/gamemodes.json", encoding="utf-8") as f:
                translate = json.loads(f.read())

                if name in translate:
                    name = translate[name]

        map = self.getMapNameById(mapId)

        return {"map": map, "name": name}

    def getMapIconByMapId(self, mapId, win):
        result = "victory" if win else "defeat"
        if mapId == 11:
            mapName = "sr"
        elif mapId == 12:
            mapName = "ha"
        elif mapId == 30:
            mapName = "arena"
        else:
            mapName = "other"

        return f"app/resource/images/{mapName}-{result}.png"

    def getChampionList(self):
        return [item for item in self.champions.keys()]

    def getSkinListByChampionName(self, championName):
        try:
            return [item for item in self.champions[championName]["skins"]]
        except:
            return []

    def getSkinIdByChampionAndSkinName(self, championName, skinName):
        return self.champions[championName]["skins"][skinName]

    def getChampionIdByName(self, championName):
        return self.champions[championName]["id"]


connector = LolClientConnector()
