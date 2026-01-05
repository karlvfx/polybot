"""
Comprehensive logging system for the trading bot.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog
from structlog.processors import JSONRenderer, TimeStamper, add_log_level

from src.models.schemas import SignalLog


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    """
    Configure structlog for the application.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory for log files
    """
    # Ensure log directory exists
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    
    # Configure structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class SignalLogger:
    """
    Specialized logger for trading signals.
    Writes comprehensive logs in JSON format for analysis.
    """
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger = structlog.get_logger("signal_logger")
        
        # Current day's log file
        self._current_date: Optional[str] = None
        self._current_file: Optional[Path] = None
        self._file_handle = None
    
    def _get_log_file(self) -> Path:
        """Get current day's log file, rotating if needed."""
        today = datetime.now().strftime("%Y-%m-%d")
        
        if today != self._current_date:
            # Close previous file
            if self._file_handle:
                self._file_handle.close()
            
            # Open new file
            self._current_date = today
            self._current_file = self.log_dir / f"signals_{today}.jsonl"
            self._file_handle = open(self._current_file, "a")
        
        return self._current_file
    
    def log_signal(self, signal_log: SignalLog) -> None:
        """
        Log a complete signal entry.
        
        Args:
            signal_log: The signal log to write
        """
        self._get_log_file()
        
        # Write as JSON line
        log_json = signal_log.model_dump_json()
        self._file_handle.write(log_json + "\n")
        self._file_handle.flush()
        
        # Also log to structlog
        self.logger.info(
            "signal_logged",
            signal_id=signal_log.signal_id,
            direction=signal_log.direction,
            confidence=signal_log.scoring.confidence,
            decision=signal_log.action.decision,
        )
    
    def log_rejection(
        self,
        timestamp_ms: int,
        reason: str,
        details: dict,
    ) -> None:
        """Log a rejected signal candidate."""
        self._get_log_file()
        
        rejection_log = {
            "type": "rejection",
            "timestamp_ms": timestamp_ms,
            "reason": reason,
            "details": details,
        }
        
        self._file_handle.write(json.dumps(rejection_log) + "\n")
        self._file_handle.flush()
        
        self.logger.debug("signal_rejected", reason=reason)
    
    def close(self) -> None:
        """Close log file handle."""
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None


class MetricsLogger:
    """
    Logger for system metrics and performance data.
    """
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger = structlog.get_logger("metrics_logger")
        
        # Metrics file
        self._metrics_file = self.log_dir / "metrics.jsonl"
    
    def log_metrics(
        self,
        component: str,
        metrics: dict,
    ) -> None:
        """
        Log component metrics.
        
        Args:
            component: Name of the component
            metrics: Dictionary of metrics
        """
        entry = {
            "timestamp_ms": int(time.time() * 1000),
            "component": component,
            "metrics": metrics,
        }
        
        with open(self._metrics_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def log_feed_health(
        self,
        feeds: dict[str, dict],
    ) -> None:
        """Log health status of all feeds."""
        entry = {
            "timestamp_ms": int(time.time() * 1000),
            "type": "feed_health",
            "feeds": feeds,
        }
        
        with open(self._metrics_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    def log_latency(
        self,
        operation: str,
        latency_ms: float,
        success: bool,
    ) -> None:
        """Log operation latency."""
        entry = {
            "timestamp_ms": int(time.time() * 1000),
            "type": "latency",
            "operation": operation,
            "latency_ms": latency_ms,
            "success": success,
        }
        
        with open(self._metrics_file, "a") as f:
            f.write(json.dumps(entry) + "\n")


class PerformanceTracker:
    """
    Tracks and reports trading performance.
    """
    
    def __init__(self):
        self.logger = structlog.get_logger("performance_tracker")
        
        # Stats
        self._signals_total = 0
        self._signals_by_type: dict[str, int] = {}
        self._signals_by_direction: dict[str, int] = {}
        
        self._trades_total = 0
        self._trades_won = 0
        self._trades_lost = 0
        
        self._total_profit = 0.0
        self._total_gas = 0.0
        
        self._oracle_delays: list[float] = []
        self._e2e_latencies: list[float] = []
    
    def record_signal(
        self,
        signal_type: str,
        direction: str,
    ) -> None:
        """Record a signal generated."""
        self._signals_total += 1
        self._signals_by_type[signal_type] = self._signals_by_type.get(signal_type, 0) + 1
        self._signals_by_direction[direction] = self._signals_by_direction.get(direction, 0) + 1
    
    def record_trade(
        self,
        profit: float,
        gas_cost: float,
        won: bool,
    ) -> None:
        """Record a trade outcome."""
        self._trades_total += 1
        if won:
            self._trades_won += 1
        else:
            self._trades_lost += 1
        
        self._total_profit += profit
        self._total_gas += gas_cost
    
    def record_oracle_delay(self, delay_s: float) -> None:
        """Record oracle update delay after signal."""
        self._oracle_delays.append(delay_s)
    
    def record_e2e_latency(self, latency_ms: float) -> None:
        """Record end-to-end latency."""
        self._e2e_latencies.append(latency_ms)
    
    def get_win_rate(self) -> float:
        """Get current win rate."""
        if self._trades_total == 0:
            return 0.0
        return self._trades_won / self._trades_total
    
    def get_avg_profit(self) -> float:
        """Get average profit per trade."""
        if self._trades_total == 0:
            return 0.0
        return (self._total_profit - self._total_gas) / self._trades_total
    
    def get_signal_density(self, hours: float = 24) -> float:
        """Get signals per day."""
        # This would need actual time tracking
        return self._signals_total
    
    def get_oracle_timing_stats(self) -> dict:
        """Get oracle timing statistics."""
        if not self._oracle_delays:
            return {"count": 0, "mean": 0, "p50": 0, "p90": 0}
        
        sorted_delays = sorted(self._oracle_delays)
        n = len(sorted_delays)
        
        return {
            "count": n,
            "mean": sum(sorted_delays) / n,
            "p50": sorted_delays[n // 2],
            "p90": sorted_delays[int(n * 0.9)] if n >= 10 else sorted_delays[-1],
        }
    
    def get_latency_stats(self) -> dict:
        """Get E2E latency statistics."""
        if not self._e2e_latencies:
            return {"count": 0, "mean": 0, "p95": 0}
        
        sorted_lats = sorted(self._e2e_latencies)
        n = len(sorted_lats)
        
        return {
            "count": n,
            "mean": sum(sorted_lats) / n,
            "p95": sorted_lats[int(n * 0.95)] if n >= 20 else sorted_lats[-1],
        }
    
    def get_summary(self) -> dict:
        """Get complete performance summary."""
        return {
            "signals": {
                "total": self._signals_total,
                "by_type": self._signals_by_type,
                "by_direction": self._signals_by_direction,
            },
            "trades": {
                "total": self._trades_total,
                "won": self._trades_won,
                "lost": self._trades_lost,
                "win_rate": self.get_win_rate(),
            },
            "profit": {
                "total": self._total_profit,
                "gas": self._total_gas,
                "net": self._total_profit - self._total_gas,
                "avg_per_trade": self.get_avg_profit(),
            },
            "oracle_timing": self.get_oracle_timing_stats(),
            "latency": self.get_latency_stats(),
        }
    
    def print_report(self) -> None:
        """Print formatted performance report."""
        summary = self.get_summary()
        oracle = summary["oracle_timing"]
        latency = summary["latency"]
        
        print("""
╔════════════════════════════════════════════════════════════╗
║                 PERFORMANCE REPORT                         ║
╠════════════════════════════════════════════════════════════╣
║ SIGNALS                                                    ║
║ ───────                                                    ║
║ Total:           {total:>6}                                 ║
║ Standard:        {standard:>6}                                 ║
║ Escape Clause:   {escape:>6}                                 ║
╠════════════════════════════════════════════════════════════╣
║ TRADES                                                     ║
║ ──────                                                     ║
║ Total:           {trades:>6}                                 ║
║ Won:             {won:>6}                                 ║
║ Lost:            {lost:>6}                                 ║
║ Win Rate:        {winrate:>5.1f}%                                ║
╠════════════════════════════════════════════════════════════╣
║ PROFIT                                                     ║
║ ──────                                                     ║
║ Gross:          €{gross:>7.2f}                              ║
║ Gas Costs:      €{gas:>7.2f}                              ║
║ Net:            €{net:>7.2f}                              ║
║ Avg/Trade:      €{avg:>7.2f}                              ║
╠════════════════════════════════════════════════════════════╣
║ ORACLE TIMING (seconds)                                    ║
║ ────────────────────────                                   ║
║ Samples:         {oracle_n:>6}                                 ║
║ Mean:            {oracle_mean:>6.1f}                                 ║
║ P90:             {oracle_p90:>6.1f}                                 ║
╠════════════════════════════════════════════════════════════╣
║ E2E LATENCY (ms)                                           ║
║ ────────────────                                           ║
║ Samples:         {lat_n:>6}                                 ║
║ Mean:            {lat_mean:>6.0f}                                 ║
║ P95:             {lat_p95:>6.0f}                                 ║
╚════════════════════════════════════════════════════════════╝
""".format(
            total=summary["signals"]["total"],
            standard=summary["signals"]["by_type"].get("standard", 0),
            escape=summary["signals"]["by_type"].get("escape_clause", 0),
            trades=summary["trades"]["total"],
            won=summary["trades"]["won"],
            lost=summary["trades"]["lost"],
            winrate=summary["trades"]["win_rate"] * 100,
            gross=summary["profit"]["total"],
            gas=summary["profit"]["gas"],
            net=summary["profit"]["net"],
            avg=summary["profit"]["avg_per_trade"],
            oracle_n=oracle["count"],
            oracle_mean=oracle["mean"],
            oracle_p90=oracle["p90"],
            lat_n=latency["count"],
            lat_mean=latency["mean"],
            lat_p95=latency["p95"],
        ))

