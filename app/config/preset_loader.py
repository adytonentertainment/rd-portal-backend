"""
Detection Preset Loader
Loads and manages detection configuration presets
"""

import json
import os
from typing import Dict, Any, Optional
from pathlib import Path


class DetectionPresetLoader:
    """Load and manage detection presets"""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize preset loader

        Args:
            config_path: Path to detection_presets.json (optional)
        """
        if config_path is None:
            # Default to config directory
            config_dir = Path(__file__).parent
            config_path = config_dir / "detection_presets.json"

        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Detection presets config not found at {self.config_path}"
            )
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in detection presets config: {e}")

    def get_preset(self, preset_name: str) -> Dict[str, Any]:
        """
        Get a specific preset configuration

        Args:
            preset_name: Name of the preset (e.g., 'budget_optimized_v1')

        Returns:
            Preset configuration dictionary

        Raises:
            KeyError: If preset not found
        """
        if preset_name not in self.config["presets"]:
            available = list(self.config["presets"].keys())
            raise KeyError(
                f"Preset '{preset_name}' not found. "
                f"Available presets: {', '.join(available)}"
            )

        return self.config["presets"][preset_name]

    def get_default_preset(self) -> Dict[str, Any]:
        """Get the default preset"""
        default_name = self.config["metadata"]["default_preset"]
        return self.get_preset(default_name)

    def list_presets(self) -> Dict[str, str]:
        """
        List all available presets

        Returns:
            Dictionary mapping preset names to descriptions
        """
        return {
            name: preset["description"]
            for name, preset in self.config["presets"].items()
        }

    def get_preset_settings(self, preset_name: str) -> Dict[str, Any]:
        """Get just the settings section of a preset"""
        preset = self.get_preset(preset_name)
        return preset["settings"]

    def get_preset_stats(self, preset_name: str) -> Dict[str, Any]:
        """Get just the performance stats of a preset"""
        preset = self.get_preset(preset_name)
        return preset["performance_stats"]

    def add_test_result(
        self,
        preset_name: str,
        track_name: str,
        confidence: float,
        detections: int,
        cost: float,
        time_seconds: float,
        result: str,
        **kwargs
    ):
        """
        Add a test result to a preset

        Args:
            preset_name: Name of the preset
            track_name: Name of the track tested
            confidence: Final confidence score
            detections: Number of detections
            cost: Cost in dollars
            time_seconds: Processing time
            result: "PASS" or "BELOW_THRESHOLD"
            **kwargs: Additional fields (type_beat, artists, notes, etc.)
        """
        preset = self.get_preset(preset_name)

        test_result = {
            "track": track_name,
            "confidence": confidence,
            "detections": detections,
            "cost": cost,
            "time_seconds": time_seconds,
            "result": result,
            **kwargs
        }

        # Add to examples
        if "examples" not in preset["test_results"]:
            preset["test_results"]["examples"] = []

        preset["test_results"]["examples"].append(test_result)

        # Update counts
        preset["test_results"]["total_tracks_tested"] = len(
            preset["test_results"]["examples"]
        )

        # Save updated config
        self._save_config()

    def update_preset_stats(
        self,
        preset_name: str,
        **stats
    ):
        """
        Update performance stats for a preset

        Args:
            preset_name: Name of the preset
            **stats: Stats to update (avg_cost_per_track, avg_processing_time_seconds, etc.)
        """
        preset = self.get_preset(preset_name)

        for key, value in stats.items():
            if key in preset["performance_stats"]:
                preset["performance_stats"][key] = value

        self._save_config()

    def create_preset(
        self,
        preset_name: str,
        name: str,
        description: str,
        settings: Dict[str, Any],
        **kwargs
    ):
        """
        Create a new preset

        Args:
            preset_name: Internal name (key)
            name: Display name
            description: Description
            settings: Settings dictionary
            **kwargs: Additional fields
        """
        if preset_name in self.config["presets"]:
            raise ValueError(f"Preset '{preset_name}' already exists")

        from datetime import date

        new_preset = {
            "name": name,
            "version": kwargs.get("version", "1.0.0"),
            "created": kwargs.get("created", date.today().isoformat()),
            "description": description,
            "settings": settings,
            "performance_stats": kwargs.get("performance_stats", {}),
            "test_results": kwargs.get("test_results", {
                "total_tracks_tested": 0,
                "tracks_above_threshold": 0,
                "tracks_below_threshold": 0,
                "examples": []
            }),
            **{k: v for k, v in kwargs.items() if k not in [
                "version", "created", "performance_stats", "test_results"
            ]}
        }

        self.config["presets"][preset_name] = new_preset
        self._save_config()

    def _save_config(self):
        """Save configuration back to JSON file"""
        from datetime import date
        self.config["metadata"]["last_updated"] = date.today().isoformat()

        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=2)

    def print_preset_summary(self, preset_name: str):
        """Print a formatted summary of a preset"""
        preset = self.get_preset(preset_name)

        print(f"\n{'='*70}")
        print(f"PRESET: {preset['name']}")
        print(f"{'='*70}")
        print(f"Version: {preset['version']}")
        print(f"Created: {preset['created']}")
        print(f"\n{preset['description']}\n")

        # Performance stats
        stats = preset['performance_stats']
        print(f"PERFORMANCE STATS:")
        print(f"  Avg Cost: ${stats['avg_cost_per_track']:.4f} per track")
        print(f"  Avg Time: {stats['avg_processing_time_seconds']}s per track")
        print(f"  Avg API Calls: {stats['avg_api_calls']}")
        print(f"  Tracks per $1: {stats['tracks_per_dollar']}")
        print(f"  Success Rate (98%): {stats.get('success_rate_98_percent', 'N/A')}")

        # Test results
        if preset.get('test_results'):
            results = preset['test_results']
            print(f"\nTEST RESULTS:")
            print(f"  Total Tracks Tested: {results.get('total_tracks_tested', 0)}")
            print(f"  Above Threshold: {results.get('tracks_above_threshold', 0)}")
            print(f"  Below Threshold: {results.get('tracks_below_threshold', 0)}")

        print(f"{'='*70}\n")


# Convenience function
def load_preset(preset_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Load a detection preset

    Args:
        preset_name: Name of preset, or None for default

    Returns:
        Preset configuration
    """
    loader = DetectionPresetLoader()

    if preset_name is None:
        return loader.get_default_preset()

    return loader.get_preset(preset_name)


def get_preset_settings(preset_name: Optional[str] = None) -> Dict[str, Any]:
    """Get just the settings from a preset"""
    loader = DetectionPresetLoader()

    if preset_name is None:
        preset = loader.get_default_preset()
    else:
        preset = loader.get_preset(preset_name)

    return preset["settings"]


if __name__ == "__main__":
    # Demo usage
    loader = DetectionPresetLoader()

    print("Available Presets:")
    print("=" * 70)
    for name, description in loader.list_presets().items():
        print(f"\n{name}:")
        print(f"  {description}")

    print("\n" * 2)

    # Show default preset
    loader.print_preset_summary(loader.config["metadata"]["default_preset"])
