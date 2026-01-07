"""
Session Tracker - Comprehensive session logging and summary generation.

Tracks all events during a bot session for detailed analysis on shutdown.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from collections import deque
from datetime import datetime, timedelta

import structlog

logger = structlog.get_logger()


@dataclass
class ConnectionEvent:
    """A connection/disconnection event for a feed."""
    timestamp: float
    feed_name: str
    event_type: str  # "connected", "disconnected", "reconnecting", "reconnected"
    details: Optional[str] = None
    attempt: int = 0


@dataclass
class SignalEvent:
    """A signal detection event (detected or rejected)."""
    timestamp: float
    asset: str
    event_type: str  # "detected", "rejected"
    direction: Optional[str] = None
    divergence_pct: float = 0.0
    pm_staleness_seconds: float = 0.0
    rejection_reason: Optional[str] = None
    confidence: float = 0.0
    spot_price: float = 0.0
    pm_yes_price: float = 0.0


@dataclass
class VirtualTradeEvent:
    """A virtual trade that was opened or closed."""
    timestamp: float
    position_id: str
    asset: str
    direction: str
    event_type: str  # "opened", "closed"
    
    # Entry details
    entry_price: float = 0.0
    confidence: float = 0.0
    divergence_at_entry: float = 0.0
    
    # Exit details (only for closed)
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    duration_seconds: float = 0.0
    
    # P&L
    gross_pnl_eur: float = 0.0
    total_fees_eur: float = 0.0
    net_pnl_eur: float = 0.0
    is_winner: bool = False


@dataclass
class DivergenceOpportunity:
    """A high-divergence opportunity that was observed."""
    timestamp: float
    asset: str
    divergence_pct: float
    pm_staleness_seconds: float
    direction: str
    was_traded: bool
    rejection_reason: Optional[str] = None
    spot_price: float = 0.0
    pm_yes_price: float = 0.0


class SessionTracker:
    """
    Tracks all events during a trading session.
    
    Provides detailed analysis on shutdown:
    - What trades went through and their outcomes
    - What opportunities were missed and why
    - Connection health and stability
    - Overall performance metrics
    """
    
    def __init__(self):
        self.logger = logger.bind(component="session_tracker")
        
        # Session timing
        self.session_start: float = time.time()
        self.session_end: Optional[float] = None
        
        # Events (limited to prevent memory issues)
        self.connection_events: deque[ConnectionEvent] = deque(maxlen=500)
        self.signal_events: deque[SignalEvent] = deque(maxlen=1000)
        self.trade_events: deque[VirtualTradeEvent] = deque(maxlen=500)
        self.divergence_opportunities: deque[DivergenceOpportunity] = deque(maxlen=500)
        
        # Aggregated stats
        self.rejection_counts: Dict[str, int] = {}
        self.feed_stats: Dict[str, Dict[str, Any]] = {}
        self.asset_stats: Dict[str, Dict[str, Any]] = {}
        
        # High divergence tracking
        self.max_divergence_seen: float = 0.0
        self.max_divergence_asset: str = ""
        self.max_divergence_time: float = 0.0
        
        self.logger.info("Session tracker initialized", start_time=datetime.fromtimestamp(self.session_start).isoformat())
    
    # =========================================================================
    # Event Recording
    # =========================================================================
    
    def record_connection_event(
        self,
        feed_name: str,
        event_type: str,
        details: Optional[str] = None,
        attempt: int = 0,
    ) -> None:
        """Record a connection event for a feed."""
        event = ConnectionEvent(
            timestamp=time.time(),
            feed_name=feed_name,
            event_type=event_type,
            details=details,
            attempt=attempt,
        )
        self.connection_events.append(event)
        
        # Update feed stats
        if feed_name not in self.feed_stats:
            self.feed_stats[feed_name] = {
                "connects": 0,
                "disconnects": 0,
                "reconnects": 0,
                "first_connect": None,
                "last_disconnect": None,
                "total_downtime_seconds": 0,
                "current_state": "unknown",
            }
        
        stats = self.feed_stats[feed_name]
        
        if event_type == "connected":
            stats["connects"] += 1
            if stats["first_connect"] is None:
                stats["first_connect"] = event.timestamp
            stats["current_state"] = "connected"
            
            # Calculate downtime if we were disconnected
            if stats["last_disconnect"]:
                downtime = event.timestamp - stats["last_disconnect"]
                stats["total_downtime_seconds"] += downtime
                stats["last_disconnect"] = None
                
        elif event_type == "disconnected":
            stats["disconnects"] += 1
            stats["last_disconnect"] = event.timestamp
            stats["current_state"] = "disconnected"
            
        elif event_type == "reconnecting":
            stats["reconnects"] += 1
            stats["current_state"] = "reconnecting"
            
        elif event_type == "reconnected":
            stats["connects"] += 1
            stats["current_state"] = "connected"
            
            if stats["last_disconnect"]:
                downtime = event.timestamp - stats["last_disconnect"]
                stats["total_downtime_seconds"] += downtime
                stats["last_disconnect"] = None
    
    def record_signal_detected(
        self,
        asset: str,
        direction: str,
        divergence_pct: float,
        pm_staleness_seconds: float,
        confidence: float,
        spot_price: float = 0.0,
        pm_yes_price: float = 0.0,
    ) -> None:
        """Record a detected signal."""
        event = SignalEvent(
            timestamp=time.time(),
            asset=asset,
            event_type="detected",
            direction=direction,
            divergence_pct=divergence_pct,
            pm_staleness_seconds=pm_staleness_seconds,
            confidence=confidence,
            spot_price=spot_price,
            pm_yes_price=pm_yes_price,
        )
        self.signal_events.append(event)
        
        # Record as traded opportunity
        opportunity = DivergenceOpportunity(
            timestamp=time.time(),
            asset=asset,
            divergence_pct=divergence_pct,
            pm_staleness_seconds=pm_staleness_seconds,
            direction=direction,
            was_traded=True,
            spot_price=spot_price,
            pm_yes_price=pm_yes_price,
        )
        self.divergence_opportunities.append(opportunity)
        
        # Update asset stats
        self._update_asset_stats(asset, "signals_detected", 1)
    
    def record_signal_rejected(
        self,
        asset: str,
        rejection_reason: str,
        divergence_pct: float = 0.0,
        pm_staleness_seconds: float = 0.0,
        direction: Optional[str] = None,
        spot_price: float = 0.0,
        pm_yes_price: float = 0.0,
    ) -> None:
        """Record a rejected signal."""
        event = SignalEvent(
            timestamp=time.time(),
            asset=asset,
            event_type="rejected",
            direction=direction,
            divergence_pct=divergence_pct,
            pm_staleness_seconds=pm_staleness_seconds,
            rejection_reason=rejection_reason,
            spot_price=spot_price,
            pm_yes_price=pm_yes_price,
        )
        self.signal_events.append(event)
        
        # Track rejection reasons
        self.rejection_counts[rejection_reason] = self.rejection_counts.get(rejection_reason, 0) + 1
        
        # Update asset stats
        self._update_asset_stats(asset, "signals_rejected", 1)
        
        # Track high divergence opportunities that were missed
        if divergence_pct >= 0.10:  # 10%+ divergence is significant
            opportunity = DivergenceOpportunity(
                timestamp=time.time(),
                asset=asset,
                divergence_pct=divergence_pct,
                pm_staleness_seconds=pm_staleness_seconds,
                direction=direction or "unknown",
                was_traded=False,
                rejection_reason=rejection_reason,
                spot_price=spot_price,
                pm_yes_price=pm_yes_price,
            )
            self.divergence_opportunities.append(opportunity)
            
            # Track max divergence
            if divergence_pct > self.max_divergence_seen:
                self.max_divergence_seen = divergence_pct
                self.max_divergence_asset = asset
                self.max_divergence_time = time.time()
    
    def record_trade_opened(
        self,
        position_id: str,
        asset: str,
        direction: str,
        entry_price: float,
        confidence: float,
        divergence_at_entry: float = 0.0,
    ) -> None:
        """Record a virtual trade being opened."""
        event = VirtualTradeEvent(
            timestamp=time.time(),
            position_id=position_id,
            asset=asset,
            direction=direction,
            event_type="opened",
            entry_price=entry_price,
            confidence=confidence,
            divergence_at_entry=divergence_at_entry,
        )
        self.trade_events.append(event)
        
        self._update_asset_stats(asset, "trades_opened", 1)
    
    def record_trade_closed(
        self,
        position_id: str,
        asset: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        exit_reason: str,
        duration_seconds: float,
        gross_pnl_eur: float,
        total_fees_eur: float,
        net_pnl_eur: float,
    ) -> None:
        """Record a virtual trade being closed."""
        is_winner = net_pnl_eur > 0
        
        event = VirtualTradeEvent(
            timestamp=time.time(),
            position_id=position_id,
            asset=asset,
            direction=direction,
            event_type="closed",
            entry_price=entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
            duration_seconds=duration_seconds,
            gross_pnl_eur=gross_pnl_eur,
            total_fees_eur=total_fees_eur,
            net_pnl_eur=net_pnl_eur,
            is_winner=is_winner,
        )
        self.trade_events.append(event)
        
        self._update_asset_stats(asset, "trades_closed", 1)
        if is_winner:
            self._update_asset_stats(asset, "trades_won", 1)
        else:
            self._update_asset_stats(asset, "trades_lost", 1)
        self._update_asset_stats(asset, "total_pnl", net_pnl_eur, is_sum=True)
    
    def _update_asset_stats(self, asset: str, key: str, value: Any, is_sum: bool = False) -> None:
        """Update asset-level statistics."""
        if asset not in self.asset_stats:
            self.asset_stats[asset] = {
                "signals_detected": 0,
                "signals_rejected": 0,
                "trades_opened": 0,
                "trades_closed": 0,
                "trades_won": 0,
                "trades_lost": 0,
                "total_pnl": 0.0,
            }
        
        if is_sum:
            self.asset_stats[asset][key] = self.asset_stats[asset].get(key, 0) + value
        else:
            self.asset_stats[asset][key] = self.asset_stats[asset].get(key, 0) + value
    
    # =========================================================================
    # Summary Generation
    # =========================================================================
    
    def generate_summary(self) -> Dict[str, Any]:
        """Generate a comprehensive session summary."""
        self.session_end = time.time()
        duration = self.session_end - self.session_start
        
        # Count trade outcomes
        closed_trades = [e for e in self.trade_events if e.event_type == "closed"]
        winning_trades = [t for t in closed_trades if t.is_winner]
        losing_trades = [t for t in closed_trades if not t.is_winner]
        
        # Calculate P&L
        total_gross_pnl = sum(t.gross_pnl_eur for t in closed_trades)
        total_fees = sum(t.total_fees_eur for t in closed_trades)
        total_net_pnl = sum(t.net_pnl_eur for t in closed_trades)
        
        # Exit reason breakdown
        exit_reasons: Dict[str, int] = {}
        for trade in closed_trades:
            reason = trade.exit_reason or "unknown"
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        
        # Connection summary
        connection_summary = {}
        for feed_name, stats in self.feed_stats.items():
            uptime_pct = 100.0
            if duration > 0:
                uptime_pct = max(0, 100 - (stats["total_downtime_seconds"] / duration * 100))
            
            connection_summary[feed_name] = {
                "connects": stats["connects"],
                "disconnects": stats["disconnects"],
                "reconnects": stats["reconnects"],
                "uptime_pct": uptime_pct,
                "total_downtime_seconds": stats["total_downtime_seconds"],
                "current_state": stats["current_state"],
            }
        
        # Missed opportunities
        missed_opportunities = [
            opp for opp in self.divergence_opportunities 
            if not opp.was_traded and opp.divergence_pct >= 0.10
        ]
        
        # Detected signals count
        detected_signals = [e for e in self.signal_events if e.event_type == "detected"]
        rejected_signals = [e for e in self.signal_events if e.event_type == "rejected"]
        
        return {
            "session": {
                "start": datetime.fromtimestamp(self.session_start).isoformat(),
                "end": datetime.fromtimestamp(self.session_end).isoformat(),
                "duration_seconds": duration,
                "duration_human": str(timedelta(seconds=int(duration))),
            },
            "signals": {
                "detected": len(detected_signals),
                "rejected": len(rejected_signals),
                "rejection_breakdown": dict(self.rejection_counts),
            },
            "trades": {
                "total": len(closed_trades),
                "winning": len(winning_trades),
                "losing": len(losing_trades),
                "win_rate": len(winning_trades) / len(closed_trades) if closed_trades else 0,
                "exit_reasons": exit_reasons,
            },
            "pnl": {
                "gross": total_gross_pnl,
                "fees": total_fees,
                "net": total_net_pnl,
                "avg_per_trade": total_net_pnl / len(closed_trades) if closed_trades else 0,
            },
            "connections": connection_summary,
            "missed_opportunities": {
                "count": len(missed_opportunities),
                "max_divergence_seen": self.max_divergence_seen,
                "max_divergence_asset": self.max_divergence_asset,
                "samples": [
                    {
                        "asset": opp.asset,
                        "divergence": f"{opp.divergence_pct:.1%}",
                        "reason": opp.rejection_reason,
                        "time": datetime.fromtimestamp(opp.timestamp).strftime("%H:%M:%S"),
                    }
                    for opp in list(missed_opportunities)[-5:]  # Last 5
                ],
            },
            "by_asset": self.asset_stats,
            "trade_details": [
                {
                    "id": t.position_id[:12],
                    "asset": t.asset,
                    "direction": t.direction,
                    "entry": f"${t.entry_price:.3f}",
                    "exit": f"${t.exit_price:.3f}" if t.exit_price else "open",
                    "exit_reason": t.exit_reason,
                    "duration": f"{t.duration_seconds:.0f}s",
                    "gross": f"€{t.gross_pnl_eur:.2f}",
                    "fees": f"€{t.total_fees_eur:.3f}",
                    "net": f"€{t.net_pnl_eur:.2f}",
                    "result": "✅" if t.is_winner else "❌",
                }
                for t in closed_trades
            ],
        }
    
    def generate_discord_report(self) -> str:
        """Generate a formatted Discord message for the session summary."""
        summary = self.generate_summary()
        
        duration = summary["session"]["duration_human"]
        
        # Build the report
        lines = [
            "# 📊 Session Summary",
            f"**Duration:** {duration}",
            "",
        ]
        
        # Signals section
        lines.append("## 🎯 Signals")
        lines.append(f"- Detected: **{summary['signals']['detected']}**")
        lines.append(f"- Rejected: **{summary['signals']['rejected']}**")
        
        if summary["signals"]["rejection_breakdown"]:
            lines.append("")
            lines.append("**Rejection Reasons:**")
            sorted_rejections = sorted(
                summary["signals"]["rejection_breakdown"].items(),
                key=lambda x: x[1],
                reverse=True,
            )
            for reason, count in sorted_rejections[:8]:  # Top 8
                lines.append(f"  • {reason}: {count}")
        
        lines.append("")
        
        # Trades section
        trades = summary["trades"]
        lines.append("## 💰 Virtual Trades")
        
        if trades["total"] > 0:
            win_rate = trades["win_rate"] * 100
            lines.append(f"- Total: **{trades['total']}** ({trades['winning']} wins, {trades['losing']} losses)")
            lines.append(f"- Win Rate: **{win_rate:.1f}%**")
            
            # P&L
            pnl = summary["pnl"]
            net_emoji = "📈" if pnl["net"] >= 0 else "📉"
            lines.append(f"- Gross P&L: €{pnl['gross']:.2f}")
            lines.append(f"- Fees Paid: €{pnl['fees']:.3f}")
            lines.append(f"- {net_emoji} **Net P&L: €{pnl['net']:.2f}**")
            lines.append(f"- Avg per Trade: €{pnl['avg_per_trade']:.2f}")
            
            # Exit reasons
            if trades["exit_reasons"]:
                lines.append("")
                lines.append("**Exit Reasons:**")
                for reason, count in trades["exit_reasons"].items():
                    emoji = "✅" if reason == "take_profit" else "⏱️" if "time" in reason else "⚠️"
                    lines.append(f"  {emoji} {reason}: {count}")
        else:
            lines.append("*No virtual trades this session*")
        
        lines.append("")
        
        # Trade details
        if summary["trade_details"]:
            lines.append("## 📋 Trade Details")
            for trade in summary["trade_details"][-10:]:  # Last 10 trades
                lines.append(
                    f"{trade['result']} **{trade['asset']} {trade['direction']}** | "
                    f"Entry: {trade['entry']} → Exit: {trade['exit']} | "
                    f"Duration: {trade['duration']} | "
                    f"Net: {trade['net']} ({trade['exit_reason']})"
                )
            lines.append("")
        
        # Missed opportunities
        missed = summary["missed_opportunities"]
        if missed["count"] > 0:
            lines.append("## ⚠️ Missed Opportunities")
            lines.append(f"- High-divergence opportunities missed: **{missed['count']}**")
            lines.append(f"- Max divergence seen: **{missed['max_divergence_seen']:.1%}** ({missed['max_divergence_asset']})")
            
            if missed["samples"]:
                lines.append("")
                lines.append("**Samples:**")
                for sample in missed["samples"]:
                    lines.append(f"  • {sample['time']}: {sample['asset']} {sample['divergence']} - {sample['reason']}")
            lines.append("")
        
        # Connection health
        lines.append("## 🔌 Connection Health")
        for feed_name, conn in summary["connections"].items():
            state_emoji = "✅" if conn["current_state"] == "connected" else "❌"
            lines.append(
                f"- **{feed_name}**: {state_emoji} {conn['uptime_pct']:.1f}% uptime "
                f"({conn['reconnects']} reconnects)"
            )
        
        lines.append("")
        lines.append("---")
        lines.append(f"*Session ended: {summary['session']['end']}*")
        
        return "\n".join(lines)
    
    def generate_compact_discord_report(self) -> str:
        """Generate a compact Discord embed-friendly summary."""
        summary = self.generate_summary()
        
        duration = summary["session"]["duration_human"]
        trades = summary["trades"]
        pnl = summary["pnl"]
        missed = summary["missed_opportunities"]
        
        # Compact format for Discord embed
        lines = []
        
        # Header
        if trades["total"] > 0:
            win_rate = trades["win_rate"] * 100
            net_emoji = "📈" if pnl["net"] >= 0 else "📉"
            lines.append(f"**Duration:** {duration}")
            lines.append(f"**Trades:** {trades['total']} ({trades['winning']}W / {trades['losing']}L) = {win_rate:.0f}% WR")
            lines.append(f"**{net_emoji} Net P&L:** €{pnl['net']:.2f} (fees: €{pnl['fees']:.2f})")
        else:
            lines.append(f"**Duration:** {duration}")
            lines.append("**Trades:** 0")
        
        # Signals
        signals = summary["signals"]
        lines.append(f"**Signals:** {signals['detected']} detected, {signals['rejected']} rejected")
        
        # Top rejection reasons
        if signals["rejection_breakdown"]:
            top_rejections = sorted(
                signals["rejection_breakdown"].items(),
                key=lambda x: x[1],
                reverse=True,
            )[:3]
            rejection_str = ", ".join(f"{r}: {c}" for r, c in top_rejections)
            lines.append(f"**Top Rejections:** {rejection_str}")
        
        # Missed opportunities
        if missed["count"] > 0:
            lines.append(f"**Missed:** {missed['count']} high-div opps (max {missed['max_divergence_seen']:.0%})")
        
        # Connection health - just show problematic ones
        problem_feeds = []
        for feed_name, conn in summary["connections"].items():
            if conn["uptime_pct"] < 99 or conn["reconnects"] > 2:
                problem_feeds.append(f"{feed_name}: {conn['uptime_pct']:.0f}%↑ {conn['reconnects']}↻")
        
        if problem_feeds:
            lines.append(f"**Connection Issues:** {', '.join(problem_feeds)}")
        else:
            lines.append("**Connections:** All stable ✅")
        
        return "\n".join(lines)


# Global session tracker instance
session_tracker = SessionTracker()

