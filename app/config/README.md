# Detection Preset Configuration System

A flexible system for managing and versioning detection algorithm configurations with performance tracking.

## 📁 Files

- **`detection_presets.json`** - Configuration file storing all presets and their stats
- **`preset_loader.py`** - Python utility for loading and managing presets
- **`README.md`** - This file

## 🎯 Available Presets

### 1. Budget Optimized v1 (Default)
**Key:** `budget_optimized_v1`

- **Cost:** $0.0475 per track (~21 tracks per $1)
- **Speed:** ~15 seconds per track
- **Success Rate:** 67% above 98% confidence, 100% correct identification
- **Best For:** Type beats, instrumentals, budget-conscious bulk scanning

### 2. Exhaustive v1
**Key:** `exhaustive_v1`

- **Cost:** $0.71 per track (~1.4 tracks per $1)
- **Speed:** ~147 seconds per track
- **Success Rate:** Very high
- **Best For:** Obscure tracks, maximum accuracy required

## 🚀 Usage

### Python Code

```python
from app.config.preset_loader import load_preset, DetectionPresetLoader

# Load default preset
preset = load_preset()

# Load specific preset
preset = load_preset("budget_optimized_v1")

# Get just settings
settings = preset["settings"]

# Use with detection
from app.services.comprehensive_detector import ComprehensiveBeatDetector

detector = ComprehensiveBeatDetector(apis)
result = detector.detect(
    audio_file,
    min_confidence=settings["confidence_calculation"]["min_confidence_threshold"]
)
```

### Viewing Presets

```python
from app.config.preset_loader import DetectionPresetLoader

loader = DetectionPresetLoader()

# List all presets
for name, description in loader.list_presets().items():
    print(f"{name}: {description}")

# View detailed summary
loader.print_preset_summary("budget_optimized_v1")

# Get performance stats
stats = loader.get_preset_stats("budget_optimized_v1")
print(f"Cost per track: ${stats['avg_cost_per_track']}")
```

### Adding Test Results

```python
loader = DetectionPresetLoader()

loader.add_test_result(
    preset_name="budget_optimized_v1",
    track_name="My Song",
    confidence=100.0,
    detections=5,
    cost=0.0475,
    time_seconds=15.2,
    result="PASS",
    type_beat=True,
    artists=["Artist1", "Artist2"]
)
```

### Creating New Presets

```python
loader = DetectionPresetLoader()

settings = {
    "sampling_strategy": {...},
    "api_strategy": {...},
    "confidence_calculation": {...}
}

loader.create_preset(
    preset_name="my_custom_preset_v1",
    name="My Custom Preset",
    description="Custom configuration for specific use case",
    settings=settings,
    version="1.0.0"
)
```

## 📊 Preset Structure

```json
{
  "preset_name": {
    "name": "Display Name",
    "version": "1.0.0",
    "created": "2025-10-20",
    "description": "What this preset does",

    "performance_stats": {
      "avg_cost_per_track": 0.0475,
      "avg_processing_time_seconds": 15,
      "avg_api_calls": 15,
      "success_rate_98_percent": "67%",
      "tracks_per_dollar": 21
    },

    "settings": {
      "sampling_strategy": {...},
      "api_strategy": {...},
      "cost_controls": {...},
      "confidence_calculation": {...},
      "type_beat_detection": {...}
    },

    "test_results": {
      "total_tracks_tested": 6,
      "examples": [...]
    },

    "implementation_notes": {
      "strengths": [...],
      "weaknesses": [...],
      "best_use_cases": [...]
    }
  }
}
```

## 🔧 Key Settings Explained

### Sampling Strategy

Defines how audio segments are selected for testing:

- **Strategic Segments** - Tests specific key parts (middle, quarter points, start, end)
- **Exhaustive** - Tests entire track with overlapping segments

### API Strategy

Controls which APIs are used and in what order:

```json
{
  "order": ["audd", "acrcloud", "acoustid"],
  "stop_on_high_confidence": true,
  "high_confidence_threshold": 95
}
```

### Confidence Calculation

How final confidence scores are computed:

```json
{
  "aggregation_method": "by_title_only",
  "bonuses": {
    "multi_detection": {
      "5_plus": 20,
      "4": 15,
      "3": 12,
      "2": 8
    },
    "multi_api": {
      "2_plus_apis": 10
    },
    "type_beat": {
      "2_plus_artists": 5
    }
  },
  "min_confidence_threshold": 98.0
}
```

## 📈 Performance Comparison

| Metric | Budget Optimized | Exhaustive |
|--------|------------------|------------|
| Cost per track | $0.0475 | $0.71 |
| Time per track | 15s | 147s |
| API calls | 15 | 138 |
| Tracks per $1 | 21 | 1.4 |
| Cost reduction | 93% | baseline |

## 🎯 When to Use Each Preset

### Budget Optimized v1
✅ Type beats and instrumentals
✅ Popular/commercial tracks
✅ Bulk scanning with budget limits
✅ Fast turnaround needed

❌ Highly obscure tracks
❌ Complex multi-sample tracks
❌ When maximum accuracy is critical

### Exhaustive v1
✅ Obscure or rare tracks
✅ Maximum accuracy required
✅ No budget constraints
✅ Complex sampling analysis

❌ Bulk operations
❌ Budget-conscious scanning
❌ Time-sensitive results

## 🔄 Versioning

Presets use semantic versioning:
- **Major** (1.x.x) - Breaking changes to algorithm or structure
- **Minor** (x.1.x) - New features, improved bonuses, new strategies
- **Patch** (x.x.1) - Bug fixes, minor tweaks

## 📝 Adding Your Own Presets

1. Create settings object with your configuration
2. Test thoroughly with representative tracks
3. Document performance stats
4. Add to `detection_presets.json` or use `create_preset()` method
5. Update this README with use cases

## 🚦 Best Practices

1. **Always test new presets** on 10+ diverse tracks before production
2. **Track performance metrics** - cost, time, accuracy
3. **Version presets** when making changes
4. **Document use cases** - when to use, when not to use
5. **Keep default preset stable** - create new versions for experiments

## 🔮 Future Enhancements

- Machine learning-based segment selection
- Genre-specific presets
- Adaptive confidence thresholds
- Real-time cost estimation
- A/B testing framework
- Performance analytics dashboard

---

**Last Updated:** 2025-10-20
**Config Version:** 1.0.0
**Default Preset:** budget_optimized_v1
