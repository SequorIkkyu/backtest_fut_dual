# Minimal backtest loop that feeds market data through strategies.

from public_tools.market import Market


class Backtest:
    def __init__(self, market: Market, strategies: list, mult: int, tick: float, parallel: bool = True):
        self.market = market
        self.strategies = strategies
        self.mult = mult
        self.tick = tick
        self.parallel = parallel

        for strategy in strategies:
            strategy.market = self.market

    def backtest(self, md, contracts: list):
        self.market.load_md(md)
        session_total_steps = len(md)

        for index, strategy in enumerate(self.strategies):
            if index < len(contracts):
                strategy.reset(contracts[index])
                strategy.session_total_steps = session_total_steps

        while True:
            md_row = self.market.step()
            if md_row is None:
                for strategy in self.strategies:
                    strategy.stop()
                break

            for strategy in self.strategies:
                strategy.step(md_row)

        pnl = 0
        fees = 0
        num_orders = 0
        num_fills = 0
        filled_qty = 0
        num_cancels = 0
        all_cycles = []

        for strategy in self.strategies:
            summary = strategy.summarize()

            if not self.parallel:
                print("\nSummary for", strategy.name, "<", strategy.contract, ">")
                print(" - PnL:", round(summary["pnl"], 1))
                print(" - Fees:", round(summary["fees"], 1))
                print(" - Orders:", round(summary["num_orders"], 1))
                print(" - Fills:", round(summary["num_fills"], 1))
                print(" - Filled Qty:", round(summary["filled_qty"], 1))
                print(" - Cancels:", round(summary["num_cancels"], 1))
                strategy.save_record()
                strategy.plot_record()

            pnl += summary["pnl"]
            fees += summary["fees"]
            num_orders += summary["num_orders"]
            num_fills += summary["num_fills"]
            filled_qty += summary["filled_qty"]
            num_cancels += summary["num_cancels"]

            if hasattr(strategy, "completed_cycles"):
                all_cycles.extend(strategy.completed_cycles)

        return pnl, fees, num_orders, num_fills, filled_qty, num_cancels, all_cycles
