import json
import os
import time
from decimal import Decimal
from typing import Dict, List, Optional, Set

import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock

from hummingbot.client import settings
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.core.event.events import BuyOrderCompletedEvent
from hummingbot.data_feed.candles_feed.candles_factory import CandlesConfig
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from hummingbot.strategy_v2.executors.dca_executor.data_types import DCAMode, DCAExecutorConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction, ExecutorAction
from decimal import Decimal
from enum import Enum
from typing import List, Literal, Optional

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.executors.data_types import ExecutorConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop


class SimpleDCAConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    markets: Dict[str, List[
        str]] = 'okx.BTC-USDT,ETH-USDT,XRP-USDT,BNB-USDT,SOL-USDT,DOGE-USDT,SHIB-USDT,TRX-USDT,ONDO-USDT,LTC-USDT,AVAX-USDT,TON-USDT,UNI-USDT,AAVE-USDT,DOT-USDT,ATOM-USDT,LINK-USDT,BAND-USDT,APT-USDT,WLD-USDT'
    exchange: str = Field(default="okx")


class DCAParams:
    price_ratio: Decimal = Decimal(0.03)
    dca_nums: int = 10
    quote_base: Decimal = Decimal(1)
    quote_multiply: Decimal = Decimal(1)
    activation_bounds: Decimal = Decimal(0.03)
    activation_price: Decimal = Decimal(0.01)
    trailing_delta: Decimal = Decimal(0.001)

    def __init__(self, d):
        for k, v in d.items():
            setattr(self, k, Decimal(str(v)))


class SimpleDCA(StrategyV2Base):
    """

    """

    # account_config_set = False
    markets: Dict[str, Set[str]]
    update_ts: float = 0
    config_spot_dict: Dict[str, DCAParams] = {}
    trading_rule_min_order_size: Dict[str, Decimal] = {}
    # @classmethod
    # def init_markets(cls, config: SimpleDCAConfig):
    #     cls.markets = {config.exchange: {config.trading_pair}}


    def __init__(self, connectors: Dict[str, ConnectorBase], config: SimpleDCAConfig):

        super().__init__(connectors, config)



    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Create actions proposal based on the current state of the executors.
        """
        spot_list_path = os.path.join(settings.CONF_DIR_PATH, 'spot_config.json')
        spot_update_ts = os.path.getmtime(spot_list_path)
        if spot_update_ts > self.update_ts:
            config_dict = {}
            with open(spot_list_path, 'r') as file:
                spot_list_json = json.load(file)
            for symbol, v in spot_list_json.items():
                config_dict[symbol] = DCAParams(v)
            self.config_spot_dict = config_dict
            self.update_ts = spot_update_ts

        create_actions = []
        for symbol, dca_params in self.config_spot_dict.items():

            active_executors_by_connector_pair = self.filter_executors(
                executors=self.get_all_executors(),
                filter_func=lambda
                    e: e.connector_name == self.config.exchange and e.trading_pair == symbol and e.is_active
            )
            if len(active_executors_by_connector_pair) == 0:
                balance = self.market_data_provider.get_balance(connector_name=self.config.exchange, asset=symbol)
                if not self.trading_rule_min_order_size.get(symbol):
                    self.trading_rule_min_order_size[symbol] = self.market_data_provider.get_trading_rules(
                            connector_name=self.config.exchange, trading_pair=symbol).min_order_size

                if balance <= self.trading_rule_min_order_size[symbol]:
                    mid_price = self.market_data_provider.get_price_by_type(connector_name=self.config.exchange,
                                                                            trading_pair=symbol,
                                                                            price_type=PriceType.MidPrice)
                    prices = [mid_price * (1 - i * dca_params.price_ratio) for i in range(int(dca_params.dca_nums))]
                    amounts_quote = [dca_params.quote_base * pow(dca_params.quote_multiply, i) for i in
                                     range(int(dca_params.dca_nums))]
                    create_actions.append(CreateExecutorAction(executor_config=DCAExecutorConfig(
                        timestamp=self.market_data_provider.time(),
                        connector_name=self.config.exchange,
                        trading_pair=symbol,
                        mode=DCAMode.MAKER,
                        side=TradeType.BUY,
                        leverage=1,
                        prices=prices,
                        amounts_quote=amounts_quote,
                        stop_loss=None,
                        take_profit=None,
                        trailing_stop=TrailingStop(activation_price=dca_params.activation_price,
                                                   trailing_delta=dca_params.trailing_delta),
                        activation_bounds=[dca_params.activation_bounds])))
            else:
                stop_ids = [e.id for e in active_executors_by_connector_pair if
                            (self.current_timestamp - e.timestamp > 60 and e.is_trading == False)]
                for stop_id in stop_ids:
                    create_actions.append(StopExecutorAction(executor_id=stop_id))

        return create_actions
