import logging
from decimal import Decimal
from typing import Dict, List, Optional

import pandas as pd

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import BuyOrderCompletedEvent, OrderFilledEvent, SellOrderCompletedEvent
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class SC_Grid(ScriptStrategyBase):
    """
    智能网格交易策略 - 事件驱动 + 主动跟随
    适用于稳定币套利场景，1tick敏感响应
    """
    
    # 核心配置
    trading_pair = "BTC-USDT"
    exchange = "bybit_testnet"
    grid_range = 5
    order_amount = Decimal(0.001)
    tick_size = Decimal("100")
    
    # 系统配置
    price_source = PriceType.MidPrice
    grid_refresh_time = 3600.0
    rebalance_refresh_time = 60.0

    markets = {exchange: {trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        
        # 网格状态
        self.n_levels = self.grid_range * 2 + 1
        self.mid_level_index = self.grid_range
        self.price_levels: List[Decimal] = []
        self.grid_price_floor = Decimal("0")
        self.grid_price_ceiling = Decimal("0")
        
        # 余额状态
        self.max_buy_levels = 0
        self.max_sell_levels = 0
        self.inventory_correct = True
        
        # 控制
        self.create_timestamp = 0
        
        self.logger().info(f"SC Grid Strategy: {self.trading_pair}, range={self.grid_range}, amount={self.order_amount}")

    # ═══════════════════════════════════════════════════════════════
    # 主循环
    # ═══════════════════════════════════════════════════════════════

    def on_tick(self):
        if self.create_timestamp > self.current_timestamp:
            return
            
        self._ensure_grid_ready()
        
        if not self._check_inventory():
            self.cancel_active_orders()
            self._schedule_next()
            return
        
        active_orders = self.get_active_orders(connector_name=self.exchange)
        
        if not active_orders:
            self._create_initial_grid()
        else:
            self._smart_monitoring()
        
        self._schedule_next()

    def _ensure_grid_ready(self):
        if not self.price_levels:
            self._update_grid()

    def _create_initial_grid(self):
        if self.max_buy_levels == 0 or self.max_sell_levels == 0:
            orders = self._create_single_side_orders()
        else:
            self._update_grid()
            orders = self._create_full_grid_orders()
        
        if orders:
            self._execute_orders(orders)

    def _smart_monitoring(self):
        if self.max_buy_levels == 0 or self.max_sell_levels == 0:
            self._active_price_following()
        else:
            self._emergency_grid_check()

    def _schedule_next(self):
        delay = self.rebalance_refresh_time if not self.inventory_correct else self.grid_refresh_time
        self.create_timestamp = self.current_timestamp + delay

    # ═══════════════════════════════════════════════════════════════
    # 网格管理
    # ═══════════════════════════════════════════════════════════════

    def _update_grid(self):
        try:
            market, trading_pair, _, _ = self.get_market_trading_pair_tuples()[0]
            mid_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, self.price_source)
            
            if not mid_price or mid_price <= 0:
                return
                
            mid_price = market.quantize_order_price(trading_pair, Decimal(mid_price))
            
            # 检查是否需要重建
            if self._should_rebuild_grid(mid_price):
                self._rebuild_grid(market, trading_pair, mid_price)
                
        except Exception as e:
            self.logger().warning(f"Grid update failed: {e}")

    def _should_rebuild_grid(self, mid_price: Decimal) -> bool:
        if not self.price_levels:
            return True
            
        current_range = self.grid_price_ceiling - self.grid_price_floor
        grid_center = self.grid_price_floor + current_range / 2
        deviation_ratio = abs(float(mid_price - grid_center)) / float(current_range)
        
        # 单侧余额时减少重建频率
        if self.max_buy_levels == 0 or self.max_sell_levels == 0:
            return deviation_ratio > 0.3
        
        return deviation_ratio > 0.1

    def _rebuild_grid(self, market, trading_pair, mid_price: Decimal):
        self.grid_price_floor = mid_price - self.tick_size * self.grid_range
        self.grid_price_ceiling = mid_price + self.tick_size * self.grid_range
        
        self.price_levels = [
            market.quantize_order_price(trading_pair, self.grid_price_floor + self.tick_size * i)
            for i in range(self.n_levels)
        ]
        
        self.mid_level_index = self.grid_range

    def _emergency_grid_check(self):
        try:
            market, trading_pair, _, _ = self.get_market_trading_pair_tuples()[0]
            current_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, self.price_source)
            
            if not current_price or not self.price_levels:
                return
                
            current_price = market.quantize_order_price(trading_pair, Decimal(current_price))
            grid_range_size = self.grid_price_ceiling - self.grid_price_floor
            grid_center = self.grid_price_floor + grid_range_size / 2
            deviation_ratio = abs(float(current_price - grid_center)) / float(grid_range_size)
            
            if deviation_ratio > 0.7:
                self.logger().warning(f"Emergency grid rebuild: deviation {deviation_ratio:.1%}")
                self._update_grid()
                
        except Exception as e:
            self.logger().warning(f"Emergency check failed: {e}")

    # ═══════════════════════════════════════════════════════════════
    # 余额管理
    # ═══════════════════════════════════════════════════════════════

    def _check_inventory(self) -> bool:
        market, _, base_asset, quote_asset = self.get_market_trading_pair_tuples()[0]
        base_balance = float(market.get_balance(base_asset))
        quote_balance = float(market.get_balance(quote_asset))
        
        self._calculate_max_levels(base_balance, quote_balance)
        
        if self.max_buy_levels > 0 or self.max_sell_levels > 0:
            self.inventory_correct = True
            return True
        else:
            self.inventory_correct = False
            self.logger().error("Insufficient balance for any orders")
            return False

    def _calculate_max_levels(self, base_balance: float, quote_balance: float):
        if not self.price_levels or self.mid_level_index >= len(self.price_levels):
            self.max_buy_levels = 0
            self.max_sell_levels = 0
            return
        
        # 计算买单能力
        available_quote = quote_balance
        max_buys = 0
        for i in range(1, self.grid_range + 1):
            level_index = self.mid_level_index - i
            if level_index < 0:
                break
            required_quote = float(self.order_amount * self.price_levels[level_index])
            if available_quote >= required_quote:
                available_quote -= required_quote
                max_buys += 1
            else:
                break
        
        # 计算卖单能力
        available_base = base_balance
        max_sells = min(self.grid_range, int(available_base // float(self.order_amount)))
        
        self.max_buy_levels = max_buys
        self.max_sell_levels = max_sells

    # ═══════════════════════════════════════════════════════════════
    # 订单管理
    # ═══════════════════════════════════════════════════════════════

    def _create_full_grid_orders(self) -> List[OrderCandidate]:
        orders = []
        
        # 买单
        buy_start = max(0, self.mid_level_index - self.max_buy_levels)
        for i in range(buy_start, self.mid_level_index):
            if not self._order_exists_at_level(i, "buy"):
                orders.append(self._create_order(i, TradeType.BUY))
        
        # 卖单
        sell_end = min(self.n_levels, self.mid_level_index + 1 + self.max_sell_levels)
        for i in range(self.mid_level_index + 1, sell_end):
            if not self._order_exists_at_level(i, "sell"):
                orders.append(self._create_order(i, TradeType.SELL))
        
        return orders

    def _create_single_side_orders(self) -> List[OrderCandidate]:
        orders = []
        
        if self.max_buy_levels > 0:
            buy_start = max(0, self.mid_level_index - self.max_buy_levels)
            for i in range(buy_start, self.mid_level_index):
                if not self._order_exists_at_level(i, "buy"):
                    orders.append(self._create_order(i, TradeType.BUY))
        
        if self.max_sell_levels > 0:
            sell_end = min(self.n_levels, self.mid_level_index + 1 + self.max_sell_levels)
            for i in range(self.mid_level_index + 1, sell_end):
                if not self._order_exists_at_level(i, "sell"):
                    orders.append(self._create_order(i, TradeType.SELL))
        
        return orders

    def _create_order(self, level: int, side: TradeType) -> OrderCandidate:
        return OrderCandidate(
            trading_pair=self.trading_pair,
            is_maker=True,
            order_type=OrderType.LIMIT_MAKER,
            order_side=side,
            amount=self.order_amount,
            price=self.price_levels[level]
        )

    def _execute_orders(self, orders: List[OrderCandidate]):
        for order in orders:
            if order.order_side == TradeType.BUY:
                self.buy(self.exchange, order.trading_pair, order.amount, order.order_type, order.price)
            else:
                self.sell(self.exchange, order.trading_pair, order.amount, order.order_type, order.price)

    def _order_exists_at_level(self, level: int, side: str) -> bool:
        if level >= len(self.price_levels):
            return False
            
        target_price = self.price_levels[level]
        active_orders = self.get_active_orders(connector_name=self.exchange)
        
        for order in active_orders:
            if abs(float(order.price) - float(target_price)) < 0.000001:
                if (side == "buy" and order.is_buy) or (side == "sell" and not order.is_buy):
                    return True
        return False

    def _count_active_orders(self, side: str) -> int:
        active_orders = self.get_active_orders(connector_name=self.exchange)
        return sum(1 for order in active_orders 
                   if (side == "buy" and order.is_buy) or (side == "sell" and not order.is_buy))

    # ═══════════════════════════════════════════════════════════════
    # 事件处理
    # ═══════════════════════════════════════════════════════════════

    def did_fill_order(self, event: OrderFilledEvent):
        self.logger().info(f"Filled: {event.trade_type.name} {event.amount:.2f} @ {event.price:.4f}")

    def did_complete_buy_order(self, event: BuyOrderCompletedEvent):
        if not self.inventory_correct:
            self.create_timestamp = self.current_timestamp + 1.0
            return
        
        filled_price = Decimal(str(event.quote_asset_amount)) / Decimal(str(event.base_asset_amount))
        self._handle_order_completion(filled_price, TradeType.BUY)

    def did_complete_sell_order(self, event: SellOrderCompletedEvent):
        if not self.inventory_correct:
            self.create_timestamp = self.current_timestamp + 1.0
            return
        
        filled_price = Decimal(str(event.quote_asset_amount)) / Decimal(str(event.base_asset_amount))
        self._handle_order_completion(filled_price, TradeType.SELL)

    def _handle_order_completion(self, filled_price: Decimal, side: TradeType):
        # 重新计算余额能力
        market, _, base_asset, quote_asset = self.get_market_trading_pair_tuples()[0]
        base_balance = float(market.get_balance(base_asset))
        quote_balance = float(market.get_balance(quote_asset))
        self._calculate_max_levels(base_balance, quote_balance)
        
        if self.max_buy_levels == 0 or self.max_sell_levels == 0:
            # 单侧余额，使用动态跟踪
            self._handle_single_side_completion(filled_price, side)
        else:
            # 双侧余额，使用传统网格
            self._update_grid()
            self._handle_dual_side_completion(filled_price, side)

    def _handle_single_side_completion(self, filled_price: Decimal, side: TradeType):
        # 放置止盈单
        self._place_profit_order(filled_price, side)
        
        # 触发智能跟随
        if side == TradeType.BUY and self.max_sell_levels == 0:
            self._smart_price_following_for_buys()
        elif side == TradeType.SELL and self.max_buy_levels == 0:
            self._smart_price_following_for_sells()

    def _handle_dual_side_completion(self, filled_price: Decimal, side: TradeType):
        # 放置止盈单
        self._place_profit_order(filled_price, side)
        
        # 调整mid_level并补充边界
        if side == TradeType.BUY:
            self.mid_level_index = max(0, self.mid_level_index - 1)
            self._add_boundary_orders(TradeType.BUY)
        else:
            self.mid_level_index = min(self.n_levels - 1, self.mid_level_index + 1)
            self._add_boundary_orders(TradeType.SELL)

    def _place_profit_order(self, filled_price: Decimal, side: TradeType):
        if side == TradeType.BUY and self.max_sell_levels > 0:
            profit_price = filled_price + self.tick_size
            profit_level = self._find_level_by_price(profit_price)
            if profit_level is not None and not self._order_exists_at_level(profit_level, "sell"):
                self._manage_order_limits("sell")
                order = self._create_order(profit_level, TradeType.SELL)
                self._execute_orders([order])
                
        elif side == TradeType.SELL and self.max_buy_levels > 0:
            profit_price = filled_price - self.tick_size
            profit_level = self._find_level_by_price(profit_price)
            if profit_level is not None and not self._order_exists_at_level(profit_level, "buy"):
                self._manage_order_limits("buy")
                order = self._create_order(profit_level, TradeType.BUY)
                self._execute_orders([order])

    def _find_level_by_price(self, price: Decimal) -> Optional[int]:
        for i, level_price in enumerate(self.price_levels):
            if abs(float(level_price) - float(price)) < 0.000001:
                return i
        return None

    def _manage_order_limits(self, side: str):
        current_orders = self._count_active_orders(side)
        if current_orders >= self.grid_range:
            if side == "sell":
                self._cancel_extreme_order("sell", max)
            else:
                self._cancel_extreme_order("buy", min)

    def _cancel_extreme_order(self, side: str, func):
        active_orders = self.get_active_orders(connector_name=self.exchange)
        target_orders = [o for o in active_orders 
                        if (side == "buy" and o.is_buy) or (side == "sell" and not o.is_buy)]
        
        if target_orders:
            extreme_order = func(target_orders, key=lambda o: float(o.price))
            self.cancel(self.exchange, extreme_order.trading_pair, extreme_order.client_order_id)

    def _add_boundary_orders(self, side: TradeType):
        if side == TradeType.BUY and self.max_buy_levels > 0:
            buy_start = max(0, self.mid_level_index - self.max_buy_levels)
            for i in range(buy_start, self.mid_level_index):
                if not self._order_exists_at_level(i, "buy"):
                    order = self._create_order(i, TradeType.BUY)
                    self._execute_orders([order])
                    break
                    
        elif side == TradeType.SELL and self.max_sell_levels > 0:
            sell_end = min(self.n_levels, self.mid_level_index + 1 + self.max_sell_levels)
            for i in range(self.mid_level_index + 1, sell_end):
                if not self._order_exists_at_level(i, "sell"):
                    order = self._create_order(i, TradeType.SELL)
                    self._execute_orders([order])
                    break

    # ═══════════════════════════════════════════════════════════════
    # 主动价格跟随
    # ═══════════════════════════════════════════════════════════════

    def _active_price_following(self):
        try:
            market, trading_pair, _, _ = self.get_market_trading_pair_tuples()[0]
            current_price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, self.price_source)
            
            if not current_price or not self.price_levels:
                return
                
            current_price = market.quantize_order_price(trading_pair, Decimal(current_price))
            
            if self.max_buy_levels > 0 and self.max_sell_levels == 0:
                self._check_price_following(current_price, "buy")
            elif self.max_sell_levels > 0 and self.max_buy_levels == 0:
                self._check_price_following(current_price, "sell")
                
        except Exception as e:
            self.logger().warning(f"Active price following failed: {e}")

    def _check_price_following(self, current_price: Decimal, side: str):
        active_orders = self.get_active_orders(connector_name=self.exchange)
        orders = [o for o in active_orders 
                 if (side == "buy" and o.is_buy) or (side == "sell" and not o.is_buy)]
        
        if len(orders) < 2:
            return
        
        orders.sort(key=lambda o: float(o.price), reverse=(side == "buy"))
        edge_price = Decimal(str(orders[0].price))
        
        if side == "buy":
            price_gap = current_price - edge_price
        else:
            price_gap = edge_price - current_price
            
        tick_gaps = int(price_gap / self.tick_size)
        
        if tick_gaps >= 1:
            self.logger().info(f"Price moved {tick_gaps} ticks, following {side}")
            self._execute_price_following(side, orders)

    def _execute_price_following(self, side: str, orders: list):
        if side == "buy":
            # 取消最低买单，添加更高买单
            lowest_order = orders[-1]
            highest_price = float(orders[0].price)
            new_price = highest_price + float(self.tick_size)
        else:
            # 取消最高卖单，添加更低卖单
            highest_order = orders[-1]
            lowest_price = float(orders[0].price)
            new_price = lowest_price - float(self.tick_size)
        
        new_level = self._find_level_by_price(Decimal(str(new_price)))
        
        if new_level is not None and not self._order_exists_at_level(new_level, side):
            # 取消边界订单
            extreme_order = orders[-1]
            self.cancel(self.exchange, extreme_order.trading_pair, extreme_order.client_order_id)
            
            # 添加新订单
            try:
                trade_type = TradeType.BUY if side == "buy" else TradeType.SELL
                order = self._create_order(new_level, trade_type)
                self._execute_orders([order])
                self.logger().info(f"Following: {side} @ {new_price:.4f}")
            except Exception as e:
                self.logger().warning(f"Following failed: {e}")

    def _smart_price_following_for_buys(self):
        # 买单成交后的智能跟随逻辑保持不变
        self._active_price_following()

    def _smart_price_following_for_sells(self):
        # 卖单成交后的智能跟随逻辑保持不变
        self._active_price_following()

    # ═══════════════════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════════════════

    def cancel_active_orders(self):
        for order in self.get_active_orders(connector_name=self.exchange):
            self.cancel(self.exchange, order.trading_pair, order.client_order_id)

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."

        lines = ["", "=== SC Grid Strategy Status ==="]
        
        lines.extend([
            f"Grid: {self.grid_range} levels, bounds: {self.grid_price_floor:.4f} - {self.grid_price_ceiling:.4f}",
            f"Mid Level: {self.mid_level_index + 1}, Capacity: {self.max_buy_levels}B/{self.max_sell_levels}S",
            f"Inventory: {'✓' if self.inventory_correct else '⚠'}"
        ])
        
        try:
            balance_df = self.get_balance_df()
            lines.extend(["", "Balances:"] + [f"  {line}" for line in balance_df.to_string(index=False).split("\n")])
        except:
            lines.append("Balance info unavailable")
        
        try:
            orders_df = self.active_orders_df()
            lines.extend(["", "Active Orders:"] + [f"  {line}" for line in orders_df.to_string(index=False).split("\n")])
        except ValueError:
            lines.append("No active orders")
        
        warnings = self.network_warning(self.get_market_trading_pair_tuples())
        warnings.extend(self.balance_warning(self.get_market_trading_pair_tuples()))
        if warnings:
            lines.extend(["", "⚠ WARNINGS:"] + [f"  {w}" for w in warnings])
        
        return "\n".join(lines)

    def grid_assets_df(self) -> pd.DataFrame:
        market, _, base_asset, quote_asset = self.get_market_trading_pair_tuples()[0]
        price = self.connectors[self.exchange].get_price_by_type(self.trading_pair, self.price_source)
        
        base_balance = float(market.get_balance(base_asset))
        quote_balance = float(market.get_balance(quote_asset))
        base_value = base_balance * float(price) if price else 0
        total_value = base_value + quote_balance
        
        return pd.DataFrame([
            ["", base_asset, quote_asset],
            ["Balance", f"{base_balance:.4f}", f"{quote_balance:.4f}"],
            ["Value", f"{base_value:.4f}", f"{quote_balance:.4f}"],
            ["Percentage", f"{base_value/total_value:.1%}" if total_value > 0 else "0%", 
             f"{quote_balance/total_value:.1%}" if total_value > 0 else "0%"]
        ]) 