"""
Time-of-Day Filtering for Win Rate Optimization.

Analyzes historical signal performance by hour of day and provides
confidence multipliers based on historical win rates.
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


class TimeOfDayAnalyzer:
    """
    Analyzes win rate by hour of day and provides confidence adjustments.
    
    Hypothesis: Win rate varies by hour due to market maker activity.
    - Night hours (2-6am) may have lower competition
    - Peak hours may have more competition but also more opportunities
    
    Usage:
        analyzer = TimeOfDayAnalyzer()
        analyzer.load_from_logs("logs/")
        
        # Get confidence multiplier for current hour
        multiplier = analyzer.get_confidence_multiplier()
        final_confidence = base_confidence * multiplier
        
        # Check if current hour is favorable
        if analyzer.is_favorable_hour():
            # Higher confidence signal
    """
    
    # Default multipliers when insufficient data
    DEFAULT_MULTIPLIER = 1.0
    UNFAVORABLE_MULTIPLIER = 0.85
    FAVORABLE_MULTIPLIER = 1.10
    
    # Minimum samples required for statistical significance
    MIN_SAMPLES_PER_HOUR = 10
    
    # Win rate thresholds
    FAVORABLE_WIN_RATE = 0.70  # 70%+ is favorable
    UNFAVORABLE_WIN_RATE = 0.55  # <55% is unfavorable
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        self.logger = logger.bind(component="time_filter")
        
        # Store statistics by hour (0-23)
        self._hour_stats: dict[int, dict] = defaultdict(
            lambda: {"wins": 0, "losses": 0, "total_profit": 0.0}
        )
        
        # Calculated metrics
        self._favorable_hours: set[int] = set()
        self._unfavorable_hours: set[int] = set()
        
        # Last analysis timestamp
        self._last_analysis_ms: int = 0
        self._analysis_interval_ms = 3600_000  # Re-analyze every hour
    
    def load_from_logs(self, log_dir: Optional[str] = None) -> int:
        """
        Load historical signal data from log files.
        
        Args:
            log_dir: Directory containing signal logs (defaults to self.log_dir)
            
        Returns:
            Number of signals loaded
        """
        log_path = Path(log_dir or self.log_dir)
        
        if not log_path.exists():
            self.logger.warning("Log directory not found", path=str(log_path))
            return 0
        
        signals_loaded = 0
        
        # Find all signal log files (signals_YYYY-MM-DD.jsonl)
        for log_file in log_path.glob("signals_*.jsonl"):
            try:
                with open(log_file, "r") as f:
                    for line in f:
                        try:
                            signal = json.loads(line.strip())
                            self._process_signal(signal)
                            signals_loaded += 1
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                self.logger.debug("Error reading log file", file=str(log_file), error=str(e))
        
        # Calculate derived metrics
        self._calculate_favorable_hours()
        self._last_analysis_ms = int(time.time() * 1000)
        
        self.logger.info(
            "Loaded historical signals",
            signals=signals_loaded,
            favorable_hours=sorted(list(self._favorable_hours)),
            unfavorable_hours=sorted(list(self._unfavorable_hours)),
        )
        
        return signals_loaded
    
    def _process_signal(self, signal: dict) -> None:
        """Process a single signal log entry."""
        timestamp_ms = signal.get("timestamp_ms", 0)
        if not timestamp_ms:
            return
        
        # Get outcome
        outcome = signal.get("outcome", {})
        if not outcome.get("filled", False):
            return  # Skip unfilled signals
        
        net_profit = outcome.get("net_profit_eur", 0.0)
        
        # Extract hour from timestamp
        dt = datetime.fromtimestamp(timestamp_ms / 1000)
        hour = dt.hour
        
        # Update stats
        if net_profit > 0:
            self._hour_stats[hour]["wins"] += 1
        else:
            self._hour_stats[hour]["losses"] += 1
        
        self._hour_stats[hour]["total_profit"] += net_profit
    
    def _calculate_favorable_hours(self) -> None:
        """Calculate which hours are favorable/unfavorable based on win rate."""
        self._favorable_hours.clear()
        self._unfavorable_hours.clear()
        
        for hour, stats in self._hour_stats.items():
            total = stats["wins"] + stats["losses"]
            
            if total < self.MIN_SAMPLES_PER_HOUR:
                continue  # Insufficient data
            
            win_rate = stats["wins"] / total
            
            if win_rate >= self.FAVORABLE_WIN_RATE:
                self._favorable_hours.add(hour)
            elif win_rate < self.UNFAVORABLE_WIN_RATE:
                self._unfavorable_hours.add(hour)
    
    def add_signal_result(
        self,
        timestamp_ms: int,
        won: bool,
        profit_eur: float = 0.0,
    ) -> None:
        """
        Add a new signal result to the statistics.
        
        Args:
            timestamp_ms: Signal timestamp
            won: Whether the signal was profitable
            profit_eur: Net profit in EUR
        """
        dt = datetime.fromtimestamp(timestamp_ms / 1000)
        hour = dt.hour
        
        if won:
            self._hour_stats[hour]["wins"] += 1
        else:
            self._hour_stats[hour]["losses"] += 1
        
        self._hour_stats[hour]["total_profit"] += profit_eur
        
        # Recalculate if interval passed
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_analysis_ms > self._analysis_interval_ms:
            self._calculate_favorable_hours()
            self._last_analysis_ms = now_ms
    
    def get_win_rate(self, hour: Optional[int] = None) -> float:
        """
        Get win rate for a specific hour.
        
        Args:
            hour: Hour (0-23), defaults to current hour
            
        Returns:
            Win rate (0.0 - 1.0), or 0.65 if insufficient data
        """
        if hour is None:
            hour = datetime.now().hour
        
        stats = self._hour_stats.get(hour)
        if not stats:
            return 0.65  # Default target win rate
        
        total = stats["wins"] + stats["losses"]
        if total < self.MIN_SAMPLES_PER_HOUR:
            return 0.65  # Default if insufficient data
        
        return stats["wins"] / total
    
    def get_confidence_multiplier(self, hour: Optional[int] = None) -> float:
        """
        Get confidence multiplier for a specific hour.
        
        Args:
            hour: Hour (0-23), defaults to current hour
            
        Returns:
            Multiplier (e.g., 1.1 for favorable, 0.85 for unfavorable)
        """
        if hour is None:
            hour = datetime.now().hour
        
        if hour in self._favorable_hours:
            return self.FAVORABLE_MULTIPLIER
        elif hour in self._unfavorable_hours:
            return self.UNFAVORABLE_MULTIPLIER
        else:
            return self.DEFAULT_MULTIPLIER
    
    def is_favorable_hour(self, hour: Optional[int] = None) -> bool:
        """
        Check if a specific hour is favorable for trading.
        
        Args:
            hour: Hour (0-23), defaults to current hour
            
        Returns:
            True if hour has historically high win rate
        """
        if hour is None:
            hour = datetime.now().hour
        
        return hour in self._favorable_hours
    
    def is_unfavorable_hour(self, hour: Optional[int] = None) -> bool:
        """
        Check if a specific hour should be avoided.
        
        Args:
            hour: Hour (0-23), defaults to current hour
            
        Returns:
            True if hour has historically low win rate
        """
        if hour is None:
            hour = datetime.now().hour
        
        return hour in self._unfavorable_hours
    
    def get_hour_stats(self, hour: Optional[int] = None) -> dict:
        """
        Get detailed statistics for a specific hour.
        
        Args:
            hour: Hour (0-23), defaults to current hour
            
        Returns:
            Dict with wins, losses, win_rate, total_profit, sample_size
        """
        if hour is None:
            hour = datetime.now().hour
        
        stats = self._hour_stats.get(hour, {"wins": 0, "losses": 0, "total_profit": 0.0})
        total = stats["wins"] + stats["losses"]
        win_rate = stats["wins"] / total if total > 0 else 0.0
        
        return {
            "hour": hour,
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": win_rate,
            "total_profit": stats["total_profit"],
            "sample_size": total,
            "is_favorable": hour in self._favorable_hours,
            "is_unfavorable": hour in self._unfavorable_hours,
            "confidence_multiplier": self.get_confidence_multiplier(hour),
        }
    
    def get_all_hours_stats(self) -> list[dict]:
        """
        Get statistics for all hours.
        
        Returns:
            List of hour stats dictionaries sorted by hour
        """
        return [self.get_hour_stats(h) for h in range(24)]
    
    def get_favorable_hours(self) -> list[int]:
        """Get list of favorable hours sorted."""
        return sorted(list(self._favorable_hours))
    
    def get_unfavorable_hours(self) -> list[int]:
        """Get list of unfavorable hours sorted."""
        return sorted(list(self._unfavorable_hours))
    
    def get_best_hours(self, n: int = 5) -> list[tuple[int, float]]:
        """
        Get the N best hours by win rate.
        
        Args:
            n: Number of hours to return
            
        Returns:
            List of (hour, win_rate) tuples sorted by win rate descending
        """
        hour_win_rates = []
        
        for hour in range(24):
            stats = self._hour_stats.get(hour)
            if not stats:
                continue
            
            total = stats["wins"] + stats["losses"]
            if total < self.MIN_SAMPLES_PER_HOUR:
                continue
            
            win_rate = stats["wins"] / total
            hour_win_rates.append((hour, win_rate))
        
        hour_win_rates.sort(key=lambda x: x[1], reverse=True)
        return hour_win_rates[:n]
    
    def generate_report(self) -> str:
        """Generate a human-readable report of time-of-day analysis."""
        lines = [
            "╔══════════════════════════════════════════════════════════════╗",
            "║           TIME-OF-DAY WIN RATE ANALYSIS                      ║",
            "╠══════════════════════════════════════════════════════════════╣",
        ]
        
        # Summary
        favorable = self.get_favorable_hours()
        unfavorable = self.get_unfavorable_hours()
        
        lines.append(f"║ Favorable Hours (≥70% win rate): {favorable or 'None yet'}".ljust(63) + "║")
        lines.append(f"║ Unfavorable Hours (<55% win rate): {unfavorable or 'None yet'}".ljust(63) + "║")
        lines.append("╠══════════════════════════════════════════════════════════════╣")
        lines.append("║ HOURLY BREAKDOWN                                             ║")
        lines.append("║ Hour | Wins | Losses | Win Rate | Profit  | Status           ║")
        lines.append("║──────┼──────┼────────┼──────────┼─────────┼──────────────────║")
        
        for hour in range(24):
            stats = self.get_hour_stats(hour)
            
            if stats["sample_size"] == 0:
                status = "No data"
            elif stats["is_favorable"]:
                status = "★ FAVORABLE"
            elif stats["is_unfavorable"]:
                status = "✗ AVOID"
            else:
                status = "Neutral"
            
            line = f"║  {hour:02d}  │  {stats['wins']:3d} │   {stats['losses']:3d}  │  {stats['win_rate']*100:5.1f}%  │ €{stats['total_profit']:6.2f} │ {status:16} ║"
            lines.append(line)
        
        lines.append("╚══════════════════════════════════════════════════════════════╝")
        
        return "\n".join(lines)
    
    def get_metrics(self) -> dict:
        """Get time filter metrics for monitoring."""
        total_signals = sum(
            s["wins"] + s["losses"] for s in self._hour_stats.values()
        )
        
        return {
            "total_signals_analyzed": total_signals,
            "favorable_hours": self.get_favorable_hours(),
            "unfavorable_hours": self.get_unfavorable_hours(),
            "current_hour": datetime.now().hour,
            "current_hour_favorable": self.is_favorable_hour(),
            "current_multiplier": self.get_confidence_multiplier(),
            "best_hours": self.get_best_hours(3),
        }

