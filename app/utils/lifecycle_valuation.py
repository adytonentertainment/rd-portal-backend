"""
Lifecycle-Aware Catalog Valuation System

Implements Rolling 12-Month (LTM) valuation with sophisticated lifecycle detection.
Provides accurate valuation based on song maturity stage and streaming patterns.
"""

from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum
import statistics
import math


class LifecycleStage(str, Enum):
    """Song lifecycle stages based on streaming pattern analysis"""
    VIRAL_NEW = "VIRAL_NEW"           # <6mo, exponential growth, peak not reached
    BUZZING = "BUZZING"               # Hit peak within 90d, riding momentum
    MATURING = "MATURING"             # 6-24mo, past peak, stabilizing
    EVERGREEN = "EVERGREEN"           # 2+ years, consistent, minimal variance
    DECLINING = "DECLINING"           # Clear downward trend, <70% of peak
    CATALOG = "CATALOG"               # 3+ years, stable low-level performance


@dataclass
class StreamingDataPoint:
    """Single data point from StatsCache"""
    date: datetime
    spotify_plays: int
    youtube_plays: int

    @property
    def total_plays(self) -> int:
        return self.spotify_plays + self.youtube_plays


@dataclass
class LifecycleMetrics:
    """Comprehensive metrics for lifecycle analysis"""
    lifecycle_stage: LifecycleStage
    stability_score: float              # 0-1, higher = more stable
    months_since_peak: float
    current_vs_peak_ratio: float        # 0-1, current performance vs peak
    growth_rate_90d: float              # Monthly growth rate over last 90d
    age_in_days: int
    peak_date: datetime
    peak_monthly_revenue: float
    current_monthly_avg: float
    coefficient_variation: float        # Lower = more stable
    confidence: str                     # "high", "medium", "low"


@dataclass
class ValuationResult:
    """Complete valuation result with lifecycle context"""
    base_revenue: float                 # LTM or appropriate window
    multiple: float                     # Lifecycle-adjusted multiple
    catalog_value: float                # Final valuation
    confidence: str                     # "high", "medium", "low"
    lifecycle: LifecycleStage
    metrics: LifecycleMetrics
    breakdown: Dict[str, float]         # Detailed multiple breakdown
    explanation: str                    # Human-readable explanation


class LifecycleValuationEngine:
    """
    Core engine for lifecycle-aware catalog valuation.

    Uses Rolling 12-Month (LTM) approach with intelligent lifecycle detection
    to provide accurate valuations based on streaming maturity patterns.
    """

    def __init__(self, master_rate: float = 0.0038, pub_rate: float = 0.001):
        self.master_rate = master_rate
        self.pub_rate = pub_rate
        self.base_multiple = 10.0

    def _filter_outliers(self, data: List[StreamingDataPoint]) -> List[StreamingDataPoint]:
        """
        Filter out data corruption outliers (>50x sudden growth).

        Common causes:
        - Multiple track versions combined into one data point
        - API errors returning wrong data
        - Database corruption
        - YouTube backfill with cumulative totals
        """
        if len(data) < 2:
            return data

        filtered = [data[0]]  # Always keep first point

        for i in range(1, len(data)):
            prev = data[i-1]
            curr = data[i]

            is_outlier = False

            # Check total plays for absurd spikes (>50x)
            if prev.total_plays > 0 and curr.total_plays > prev.total_plays * 50:
                is_outlier = True

            # Check individual platforms for absurd spikes
            # This catches YouTube backfill issues where YT jumps from 2M to 1.2B
            if prev.spotify_plays > 0 and curr.spotify_plays > prev.spotify_plays * 50:
                is_outlier = True

            if prev.youtube_plays > 0 and curr.youtube_plays > prev.youtube_plays * 50:
                is_outlier = True

            # Also check for massive absolute jumps (>1B plays in one day = definitely wrong)
            daily_growth = curr.total_plays - prev.total_plays
            if daily_growth > 1_000_000_000:  # 1 billion plays in one period
                is_outlier = True

            if is_outlier:
                continue  # Skip this corrupted data point

            filtered.append(data[i])

        return filtered

    def calculate_song_valuation(
        self,
        historical_data: List[StreamingDataPoint],
        release_date: datetime,
        master_royalty: float,
        pub_royalty: float
    ) -> ValuationResult:
        """
        Calculate comprehensive valuation for a single song.

        Args:
            historical_data: List of streaming data points (must be sorted by date ASC)
            release_date: Song release date
            master_royalty: Master ownership percentage (0-1)
            pub_royalty: Publishing ownership percentage (0-1)

        Returns:
            ValuationResult with complete lifecycle analysis
        """
        if not historical_data or len(historical_data) < 2:
            return self._create_zero_valuation()

        # Filter outliers: Remove data points with >50x sudden growth (data corruption)
        filtered_data = self._filter_outliers(historical_data)

        if len(filtered_data) < 2:
            return self._create_zero_valuation()

        # Detect lifecycle stage and calculate metrics
        metrics = self._detect_lifecycle(filtered_data, release_date)

        # Choose appropriate revenue calculation window
        base_revenue = self._calculate_base_revenue(
            filtered_data, metrics, master_royalty, pub_royalty
        )

        # Calculate lifecycle-adjusted multiple
        multiple_breakdown = self._calculate_lifecycle_multiple(metrics)
        final_multiple = multiple_breakdown['final_multiple']

        # Calculate final valuation
        catalog_value = base_revenue * final_multiple

        # Safeguard: Never return negative valuations
        if catalog_value < 0:
            catalog_value = 0
        if base_revenue < 0:
            base_revenue = 0

        # Sanity check: Cap single song valuation at $100M (extremely generous)
        # Even "Blinding Lights" one of the most streamed songs ever is worth ~$50M
        max_song_value = 100_000_000
        if catalog_value > max_song_value:
            catalog_value = max_song_value
            # Adjust base_revenue proportionally for consistency
            if final_multiple > 0:
                base_revenue = catalog_value / final_multiple

        # Generate explanation
        explanation = self._generate_explanation(metrics, multiple_breakdown)

        return ValuationResult(
            base_revenue=base_revenue,
            multiple=final_multiple,
            catalog_value=catalog_value,
            confidence=metrics.confidence,
            lifecycle=metrics.lifecycle_stage,
            metrics=metrics,
            breakdown=multiple_breakdown,
            explanation=explanation
        )

    def _detect_lifecycle(
        self,
        data: List[StreamingDataPoint],
        release_date: datetime
    ) -> LifecycleMetrics:
        """
        Detect lifecycle stage using comprehensive pattern analysis.

        This is the CORE of the valuation system - accurate lifecycle detection
        is critical for proper valuation.
        """
        now = datetime.now()
        age_days = (now - release_date).days
        age_months = age_days / 30.44

        # Calculate monthly aggregates for pattern analysis
        monthly_data = self._aggregate_monthly(data)

        # Find peak month
        peak_month = max(monthly_data.items(), key=lambda x: x[1]['revenue'])
        peak_date = peak_month[0]
        peak_revenue = peak_month[1]['revenue']

        # Calculate months since peak
        months_since_peak = (now - peak_date).days / 30.44

        # Current monthly average (last 30 days)
        current_monthly_avg = self._calculate_recent_monthly_avg(data, days=30)

        # Current vs peak ratio
        current_vs_peak = current_monthly_avg / peak_revenue if peak_revenue > 0 else 0

        # Growth rate (last 90 days)
        growth_rate_90d = self._calculate_growth_rate(data, days=90)

        # Stability metrics
        stability_score = self._calculate_stability_score(monthly_data)
        cv = self._calculate_coefficient_variation(monthly_data)

        # LIFECYCLE DETECTION LOGIC
        lifecycle_stage = self._determine_lifecycle_stage(
            age_months=age_months,
            months_since_peak=months_since_peak,
            current_vs_peak=current_vs_peak,
            growth_rate_90d=growth_rate_90d,
            stability_score=stability_score,
            is_still_growing=self._is_still_growing(data)
        )

        # Data quality check: Flag suspicious viral growth with limited data
        data_quality_suspect = False
        data_span_days = (data[-1].date - data[0].date).days if len(data) > 1 else 0

        if (lifecycle_stage == LifecycleStage.VIRAL_NEW and
            len(data) < 30 and
            data_span_days < 30 and
            growth_rate_90d > 10.0):  # 1000%+ monthly growth
            data_quality_suspect = True
            # Override to MATURING with low confidence for safety
            lifecycle_stage = LifecycleStage.MATURING

        # Confidence scoring
        confidence = self._calculate_confidence(
            lifecycle_stage, len(data), age_months, stability_score
        )

        # Force low confidence if data quality is suspect
        if data_quality_suspect:
            confidence = "low"

        return LifecycleMetrics(
            lifecycle_stage=lifecycle_stage,
            stability_score=stability_score,
            months_since_peak=months_since_peak,
            current_vs_peak_ratio=current_vs_peak,
            growth_rate_90d=growth_rate_90d,
            age_in_days=age_days,
            peak_date=peak_date,
            peak_monthly_revenue=peak_revenue,
            current_monthly_avg=current_monthly_avg,
            coefficient_variation=cv,
            confidence=confidence
        )

    def _determine_lifecycle_stage(
        self,
        age_months: float,
        months_since_peak: float,
        current_vs_peak: float,
        growth_rate_90d: float,
        stability_score: float,
        is_still_growing: bool
    ) -> LifecycleStage:
        """
        Core lifecycle detection logic following the specification.
        """
        # VIRAL_NEW: Released <6 months, exponential growth, peak not yet reached
        if age_months < 6:
            if is_still_growing and growth_rate_90d > 0.20:  # 20%+ growth
                return LifecycleStage.VIRAL_NEW
            if current_vs_peak > 0.8:
                return LifecycleStage.BUZZING
            return LifecycleStage.MATURING

        # BUZZING: Just hit peak (within last 90 days)
        if months_since_peak < 3 and current_vs_peak > 0.8:
            return LifecycleStage.BUZZING

        # DECLINING: Clear downward trend, recent avg < 70% of peak
        if current_vs_peak < 0.7:
            # Check if it's old but stable (CATALOG) or truly declining
            if age_months > 36 and stability_score > 0.7:
                return LifecycleStage.CATALOG
            return LifecycleStage.DECLINING

        # EVERGREEN: 2+ years old, consistent revenue, minimal variance
        if age_months > 24 and stability_score > 0.85:
            return LifecycleStage.EVERGREEN

        # CATALOG: 3+ years old, stable low-level consistent performance
        if age_months > 36 and stability_score > 0.7:
            return LifecycleStage.CATALOG

        # MATURING: Post-peak, settling into baseline
        if months_since_peak > 6 and current_vs_peak < 0.4:
            if stability_score > 0.7:
                return LifecycleStage.CATALOG
            return LifecycleStage.MATURING

        # Default: MATURING (transition period)
        return LifecycleStage.MATURING

    def _calculate_base_revenue(
        self,
        data: List[StreamingDataPoint],
        metrics: LifecycleMetrics,
        master_royalty: float,
        pub_royalty: float
    ) -> float:
        """
        Calculate base revenue using appropriate time window for lifecycle stage.

        Critical: Don't use LTM blindly!
        - BUZZING: Use last 90 days (LTM understates current value)
        - DECLINING: Use last 90 days (LTM overstates current value)
        - VIRAL_NEW: Project based on trajectory
        - Others: Standard LTM

        Revenue calculation per million streams:
        - Master: 1M streams × master_royalty × $0.0038 = revenue
        - Publishing: 1M streams × pub_royalty × $0.001 = revenue
        """
        lifecycle = metrics.lifecycle_stage

        if lifecycle == LifecycleStage.BUZZING or lifecycle == LifecycleStage.DECLINING:
            # Use recent 90-day average, annualized (returns plays/year)
            annual_plays = self._calculate_recent_annual_plays(data, days=90)

        elif lifecycle == LifecycleStage.VIRAL_NEW:
            # Project next year based on growth trajectory (returns plays/year)
            annual_plays = self._project_viral_plays(data, metrics)

        else:
            # Standard LTM (Last 12 Months) (returns plays/year)
            annual_plays = self._calculate_ltm_plays(data)

        # Apply correct revenue formula: plays × (master_ownership × master_rate + pub_ownership × pub_rate)
        total_revenue = annual_plays * (master_royalty * self.master_rate + pub_royalty * self.pub_rate)

        return total_revenue

    def _calculate_lifecycle_multiple(self, metrics: LifecycleMetrics) -> Dict[str, float]:
        """
        Calculate lifecycle-adjusted valuation multiple.

        Lifecycle Adjustments:
        - VIRAL_NEW: 1.5-2x (high growth, unproven)
        - BUZZING: 1.3x (peak momentum)
        - MATURING: 0.85x (transitioning down)
        - EVERGREEN: 1.2-1.4x (stable gold)
        - DECLINING: 0.5-0.7x (fading)
        - CATALOG: 1.1x (proven longevity)
        """
        base = self.base_multiple
        lifecycle = metrics.lifecycle_stage

        # Lifecycle adjustment
        if lifecycle == LifecycleStage.VIRAL_NEW:
            lifecycle_mult = 1.75 if metrics.growth_rate_90d > 0.30 else 1.5
        elif lifecycle == LifecycleStage.BUZZING:
            lifecycle_mult = 1.3
        elif lifecycle == LifecycleStage.MATURING:
            lifecycle_mult = 0.85
        elif lifecycle == LifecycleStage.EVERGREEN:
            # Higher stability = higher multiple
            lifecycle_mult = 1.2 + (metrics.stability_score * 0.2)  # 1.2-1.4x
        elif lifecycle == LifecycleStage.DECLINING:
            # Severity of decline affects multiple
            lifecycle_mult = 0.5 + (metrics.current_vs_peak_ratio * 0.2)  # 0.5-0.7x
        else:  # CATALOG
            lifecycle_mult = 1.1

        # Stability bonus (for all stages)
        stability_bonus = 0.2 if metrics.coefficient_variation < 0.15 else 0

        # Recency bonus (growing vs declining)
        recency_bonus = 0.1 if metrics.growth_rate_90d > 0.05 else 0

        # Consistency bonus (no month below 70% of average)
        consistency_bonus = 0.15 if metrics.stability_score > 0.9 else 0

        # Calculate final multiple
        final_multiple = base * lifecycle_mult + stability_bonus + recency_bonus + consistency_bonus

        return {
            'base_multiple': base,
            'lifecycle_adjustment': lifecycle_mult,
            'stability_bonus': stability_bonus,
            'recency_bonus': recency_bonus,
            'consistency_bonus': consistency_bonus,
            'final_multiple': final_multiple
        }

    def _aggregate_monthly(self, data: List[StreamingDataPoint]) -> Dict[datetime, Dict[str, Any]]:
        """Aggregate data by month for pattern analysis"""
        monthly = {}

        for point in data:
            month_key = datetime(point.date.year, point.date.month, 1)
            if month_key not in monthly:
                monthly[month_key] = {
                    'plays': 0,
                    'revenue': 0,
                    'data_points': 0
                }

            plays = point.total_plays
            # Estimate revenue using average rate
            revenue = plays * ((self.master_rate + self.pub_rate) / 2)

            monthly[month_key]['plays'] += plays
            monthly[month_key]['revenue'] += revenue
            monthly[month_key]['data_points'] += 1

        return monthly

    def _calculate_recent_annual_plays(self, data: List[StreamingDataPoint], days: int) -> float:
        """Calculate annual plays based on recent period growth rate"""
        cutoff = datetime.now() - timedelta(days=days)
        recent_data = [d for d in data if d.date >= cutoff]

        if not recent_data or len(recent_data) < 2:
            return 0

        first = recent_data[0]
        last = recent_data[-1]

        plays_growth = last.total_plays - first.total_plays
        days_span = (last.date - first.date).days

        if days_span == 0:
            return 0

        daily_plays = plays_growth / days_span
        annual_plays = daily_plays * 365  # Annual projection

        return annual_plays

    def _calculate_recent_monthly_avg(self, data: List[StreamingDataPoint], days: int) -> float:
        """Calculate average monthly revenue for recent period (used for lifecycle detection only)"""
        annual_plays = self._calculate_recent_annual_plays(data, days)
        monthly_plays = annual_plays / 12

        # Estimate revenue using average rate (for lifecycle detection)
        avg_rate = (self.master_rate + self.pub_rate) / 2
        return monthly_plays * avg_rate

    def _calculate_growth_rate(self, data: List[StreamingDataPoint], days: int) -> float:
        """Calculate monthly growth rate over specified period"""
        cutoff = datetime.now() - timedelta(days=days)
        period_data = [d for d in data if d.date >= cutoff]

        if len(period_data) < 2:
            return 0

        first = period_data[0]
        last = period_data[-1]

        if first.total_plays == 0:
            return 0

        months = (last.date - first.date).days / 30.44
        if months == 0:
            return 0

        total_growth = (last.total_plays - first.total_plays) / first.total_plays
        monthly_growth = total_growth / months

        return monthly_growth

    def _calculate_stability_score(self, monthly_data: Dict[datetime, Dict[str, Any]]) -> float:
        """
        Calculate stability score (0-1).
        Based on how consistent monthly revenues are.
        """
        if len(monthly_data) < 3:
            return 0.5

        revenues = [m['revenue'] for m in monthly_data.values()]

        if not revenues or max(revenues) == 0:
            return 0.5

        # Calculate coefficient of variation
        cv = self._calculate_coefficient_variation(monthly_data)

        # Convert CV to stability score (inverse relationship)
        # CV < 0.1 = very stable (0.95)
        # CV > 1.0 = very unstable (0.3)
        stability = max(0.3, min(0.95, 1.0 - (cv * 0.65)))

        return stability

    def _calculate_coefficient_variation(self, monthly_data: Dict[datetime, Dict[str, Any]]) -> float:
        """Calculate coefficient of variation for monthly revenues"""
        revenues = [m['revenue'] for m in monthly_data.values() if m['revenue'] > 0]

        if len(revenues) < 2:
            return 1.0

        mean = statistics.mean(revenues)
        if mean == 0:
            return 1.0

        stdev = statistics.stdev(revenues)
        cv = stdev / mean

        return cv

    def _is_still_growing(self, data: List[StreamingDataPoint]) -> bool:
        """Check if song is still in growth phase (not peaked yet)"""
        if len(data) < 4:
            return True

        # Check if last 25% of data shows higher average than first 75%
        split_point = int(len(data) * 0.75)
        early_data = data[:split_point]
        recent_data = data[split_point:]

        early_avg = sum(d.total_plays for d in early_data) / len(early_data)
        recent_avg = sum(d.total_plays for d in recent_data) / len(recent_data)

        return recent_avg > early_avg * 1.1

    def _calculate_ltm_plays(self, data: List[StreamingDataPoint]) -> float:
        """
        Calculate Last 12 Months actual play growth (not a projection).
        This returns the ACTUAL number of plays gained in the last 365 days.
        """
        cutoff = datetime.now() - timedelta(days=365)
        ltm_data = [d for d in data if d.date >= cutoff]

        if len(ltm_data) < 2:
            # Not enough LTM data, fall back to all available data
            if len(data) < 2:
                return 0
            ltm_data = data

        first = ltm_data[0]
        last = ltm_data[-1]

        # Return ACTUAL play growth over this period
        actual_plays = last.total_plays - first.total_plays

        # Annualize if the period is shorter than 365 days
        days_span = (last.date - first.date).days
        if days_span > 0 and days_span < 365:
            annual_plays = actual_plays * (365 / days_span)
        else:
            annual_plays = actual_plays

        return annual_plays

    def _project_viral_plays(self, data: List[StreamingDataPoint], metrics: LifecycleMetrics) -> float:
        """Project next 12 months plays for viral new tracks (not revenue)"""
        if not data or len(data) < 2:
            return 0

        # Calculate current daily plays rate
        recent_data = data[-30:] if len(data) >= 30 else data
        if len(recent_data) < 2:
            return 0

        first = recent_data[0]
        last = recent_data[-1]

        plays_growth = last.total_plays - first.total_plays
        days_span = (last.date - first.date).days

        if days_span == 0:
            return 0

        daily_plays = plays_growth / days_span
        current_monthly_plays = daily_plays * 30.44

        # Get growth rate - CAP to reasonable maximum (200% monthly = 2.0)
        # Even viral hits rarely sustain >100% monthly growth
        growth_rate = min(metrics.growth_rate_90d, 2.0)

        # Project with growth but apply diminishing returns
        projected_months = []
        for month in range(12):
            # Growth decays over time (viral songs plateau)
            decay_factor = 0.9 ** month
            month_growth = growth_rate * decay_factor
            # Use compound growth properly: base * (1 + rate)^time
            projected = current_monthly_plays * ((1 + month_growth) ** (month + 1))
            projected_months.append(projected)

        total_projected = sum(projected_months)

        # Sanity check: Cap at 10 billion plays/year (no song does more than this)
        max_annual_plays = 10_000_000_000
        return min(total_projected, max_annual_plays)

    def _calculate_confidence(
        self,
        lifecycle: LifecycleStage,
        data_points: int,
        age_months: float,
        stability_score: float
    ) -> str:
        """
        Calculate confidence level for valuation.

        HIGH: Mature, stable, lots of data
        MEDIUM: Some uncertainty (new, transitioning, or moderate data)
        LOW: High uncertainty (very new, volatile, or sparse data)
        """
        # Data sufficiency
        has_sufficient_data = data_points >= 30

        # Maturity
        is_mature = age_months >= 12

        # Stability
        is_stable = stability_score > 0.75

        # Lifecycle-specific confidence
        if lifecycle == LifecycleStage.EVERGREEN:
            return "high" if has_sufficient_data else "medium"

        elif lifecycle == LifecycleStage.CATALOG:
            return "high" if has_sufficient_data else "medium"

        elif lifecycle == LifecycleStage.VIRAL_NEW:
            return "low"  # Too early to know

        elif lifecycle == LifecycleStage.BUZZING:
            return "medium"  # Need to see if it sustains

        elif lifecycle == LifecycleStage.MATURING:
            return "medium" if is_mature and has_sufficient_data else "low"

        elif lifecycle == LifecycleStage.DECLINING:
            return "medium" if has_sufficient_data else "low"

        return "medium"

    def _generate_explanation(
        self,
        metrics: LifecycleMetrics,
        breakdown: Dict[str, float]
    ) -> str:
        """Generate human-readable explanation of valuation"""
        lifecycle = metrics.lifecycle_stage

        explanations = {
            LifecycleStage.VIRAL_NEW: (
                f"Viral new track with {metrics.growth_rate_90d*100:.0f}% monthly growth. "
                f"High potential but unproven longevity. Peak not yet reached."
            ),
            LifecycleStage.BUZZING: (
                f"Buzzing track riding peak momentum ({metrics.months_since_peak:.1f} months since peak). "
                f"Currently at {metrics.current_vs_peak_ratio*100:.0f}% of peak performance. "
                f"Premium multiple for hot catalog asset."
            ),
            LifecycleStage.MATURING: (
                f"Maturing track {metrics.months_since_peak:.1f} months past peak, "
                f"settling to baseline at {metrics.current_vs_peak_ratio*100:.0f}% of peak. "
                f"Conservative multiple for transition period."
            ),
            LifecycleStage.EVERGREEN: (
                f"Evergreen track with high stability ({metrics.stability_score:.2f}) and "
                f"consistent performance {metrics.months_since_peak:.0f} months past peak. "
                f"Premium multiple for predictable income stream."
            ),
            LifecycleStage.DECLINING: (
                f"Declining track at {metrics.current_vs_peak_ratio*100:.0f}% of peak performance. "
                f"Conservative multiple reflects downward trend. "
                f"Valuation based on recent 90-day average."
            ),
            LifecycleStage.CATALOG: (
                f"Catalog track ({metrics.age_in_days/365:.1f} years old) with stable "
                f"low-level performance. Longevity bonus for proven survivor."
            ),
        }

        base_explanation = explanations.get(lifecycle, "")

        # Add confidence note
        confidence_note = f" Confidence: {metrics.confidence.upper()}."

        return base_explanation + confidence_note

    def _create_zero_valuation(self) -> ValuationResult:
        """Create zero valuation for insufficient data"""
        return ValuationResult(
            base_revenue=0,
            multiple=0,
            catalog_value=0,
            confidence="low",
            lifecycle=LifecycleStage.MATURING,
            metrics=LifecycleMetrics(
                lifecycle_stage=LifecycleStage.MATURING,
                stability_score=0,
                months_since_peak=0,
                current_vs_peak_ratio=0,
                growth_rate_90d=0,
                age_in_days=0,
                peak_date=datetime.now(),
                peak_monthly_revenue=0,
                current_monthly_avg=0,
                coefficient_variation=0,
                confidence="low"
            ),
            breakdown={'final_multiple': 0},
            explanation="Insufficient data for valuation"
        )
