import os
from decimal import Decimal
from typing import Dict, List, Optional, Set

import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
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
    markets: Dict[str, List[str]] = {}
    exchange: str = Field(default="binance_perpetual_testnet")
    # connector_name: str = Field(default="binance_perpetual_testnet")
    trading_pair: str = Field(default="BTC-USDT")
    side: TradeType = TradeType.BUY
    leverage: int = 1
    amounts_quote: List[Decimal] = [Decimal(100),Decimal(100)]
    prices: List[Decimal]  = [Decimal(96000000),Decimal(95000000)]
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    trailing_stop: Optional[TrailingStop] = TrailingStop(activation_price=Decimal("0.05"),
                                                              trailing_delta=Decimal("0.01"))
    time_limit: Optional[int] = None
    mode: DCAMode = DCAMode.MAKER
    activation_bounds: Optional[List[Decimal]] = None



class SimpleDCA(StrategyV2Base):
    """

    """

    # account_config_set = False
    markets: Dict[str, Set[str]]
    finish = False

    @classmethod
    def init_markets(cls, config: SimpleDCAConfig):
        cls.markets = {config.exchange: {config.trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase], config: SimpleDCAConfig):

        super().__init__(connectors, config)


    # def start(self, clock: Clock, timestamp: float) -> None:
    #     """
    #     Start the strategy.
    #     :param clock: Clock to use.
    #     :param timestamp: Current time.
    #     """
    #     self._last_timestamp = timestamp
    #     self.apply_initial_setting()


    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Create actions proposal based on the current state of the executors.
        """
        create_actions = []
        if not self.finish:
            create_actions.append(CreateExecutorAction(executor_config=DCAExecutorConfig(
                timestamp=self.market_data_provider.time(),
                connector_name=self.config.exchange,
                trading_pair=self.config.trading_pair,
                mode=DCAMode.MAKER,
                side=self.config.side,
                prices=self.config.prices,
                amounts_quote=self.config.amounts_quote,
                stop_loss=self.config.stop_loss,
                take_profit=self.config.take_profit,
                trailing_stop=self.config.trailing_stop)))
            self.finish = True
        return create_actions