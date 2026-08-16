"""
Global Playcount Cache Service

This service manages a global cache of historical playcount data that's not tied to users.
It fetches missing data from Songstats API and stores it for reuse across all users.
"""

from typing import List, Dict, Any, Optional
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from app.models.models import GlobalPlaycountHistory
from app.libs.Songstats import songstats_api
from app.logger import get_logger

logger = get_logger("Global Playcount Cache")

class GlobalPlaycountCacheService:
    """Service for managing global playcount historical data cache"""
    
    def __init__(self, db: Session):
        self.db = db
    
    async def ensure_historical_data(
        self, 
        track_ids: List[str], 
        start_date: date, 
        end_date: date
    ) -> Dict[str, Any]:
        """
        Ensure we have historical data for the given tracks and date range.
        Fetches missing data from Songstats API and stores it in the global cache.
        
        Args:
            track_ids: List of Spotify track IDs
            start_date: Start date for historical data
            end_date: End date for historical data
            
        Returns:
            Dict with statistics about the operation
        """
        stats = {
            "tracks_processed": 0,
            "entries_fetched": 0,
            "entries_cached": 0,
            "api_calls_made": 0,
            "errors": []
        }
        
        logger.info(f"Ensuring historical data for {len(track_ids)} tracks from {start_date} to {end_date}")
        
        for track_id in track_ids:
            try:
                track_stats = await self._ensure_track_historical_data(track_id, start_date, end_date)
                stats["tracks_processed"] += 1
                stats["entries_fetched"] += track_stats["entries_fetched"]
                stats["entries_cached"] += track_stats["entries_cached"]
                stats["api_calls_made"] += track_stats["api_calls_made"]
                
                if track_stats["error"]:
                    stats["errors"].append(f"Track {track_id}: {track_stats['error']}")
                    
            except Exception as e:
                logger.error(f"Error processing track {track_id}: {e}")
                stats["errors"].append(f"Track {track_id}: {str(e)}")
        
        logger.info(f"Historical data ensure completed: {stats}")
        return stats
    
    async def _ensure_track_historical_data(
        self, 
        track_id: str, 
        start_date: date, 
        end_date: date
    ) -> Dict[str, Any]:
        """Ensure historical data for a single track"""
        
        track_stats = {
            "entries_fetched": 0,
            "entries_cached": 0,
            "api_calls_made": 0,
            "error": None
        }
        
        try:
            # Check what data we already have in the cache
            existing_entries = self.db.query(GlobalPlaycountHistory) \
                .filter(GlobalPlaycountHistory.spotify_track_id == track_id) \
                .filter(GlobalPlaycountHistory.data_date >= start_date) \
                .filter(GlobalPlaycountHistory.data_date <= end_date) \
                .all()
            
            existing_dates = {entry.data_date for entry in existing_entries}
            logger.info(f"Track {track_id}: Found {len(existing_dates)} existing entries in cache")
            
            # Find missing dates
            missing_dates = []
            current_date = start_date
            while current_date <= end_date:
                if current_date not in existing_dates:
                    missing_dates.append(current_date)
                current_date += timedelta(days=1)
            
            if not missing_dates:
                logger.info(f"Track {track_id}: All data already cached, no API call needed")
                return track_stats
            
            logger.info(f"Track {track_id}: Missing {len(missing_dates)} dates, fetching from Songstats API")
            
            # Fetch missing data from Songstats API
            track_start_date = min(missing_dates)
            track_end_date = max(missing_dates)
            
            historical_data = await songstats_api.get_historical_data_for_date_range(
                spotify_track_id=track_id,
                start_date=track_start_date,
                end_date=track_end_date
            )
            
            track_stats["api_calls_made"] = 1
            
            if historical_data.get('history'):
                # Store the fetched data in global cache
                stored_count = 0
                
                # Debug: Check if all entries have the same playcount (straight line issue)
                playcounts = [entry.get('playcount', entry.get('streams_total', 0)) for entry in historical_data['history']]
                unique_playcounts = set(playcounts)
                
                if len(unique_playcounts) == 1:
                    logger.warning(f"STRAIGHT LINE ISSUE for {track_id}: All {len(historical_data['history'])} entries have the same playcount: {list(unique_playcounts)[0]:,}")
                elif len(unique_playcounts) < len(playcounts) / 2:
                    logger.warning(f"PARTIAL STRAIGHT LINE for {track_id}: Only {len(unique_playcounts)} unique playcounts out of {len(playcounts)} entries")
                else:
                    logger.info(f"GOOD DATA for {track_id}: {len(unique_playcounts)} unique playcounts out of {len(playcounts)} entries")
                
                # Show first few playcounts for debugging
                logger.info(f"First 5 playcounts for {track_id}: {playcounts[:5]}")
                
                for entry in historical_data['history']:
                    entry_date = datetime.strptime(entry['date'], '%Y-%m-%d').date()
                    
                    # Only store if this date was missing
                    if entry_date in missing_dates:
                        playcount_value = entry.get('playcount', entry.get('streams_total', 0))
                        
                        # Use insert...on conflict update to handle duplicates gracefully
                        try:
                            cache_entry = GlobalPlaycountHistory(
                                spotify_track_id=track_id,
                                playcount=playcount_value,
                                data_date=entry_date,
                                source='songstats'
                            )
                            self.db.add(cache_entry)
                            stored_count += 1
                            
                        except Exception as e:
                            # Handle duplicate key constraint violation
                            logger.warning(f"Duplicate entry for {track_id} on {entry_date}: {e}")
                            continue
                
                self.db.commit()
                track_stats["entries_fetched"] = len(historical_data['history'])
                track_stats["entries_cached"] = stored_count
                
                logger.info(f"Track {track_id}: Stored {stored_count} new entries in global cache")
            else:
                logger.warning(f"No history data received for track {track_id}")
                track_stats["error"] = "No history data from Songstats API"
                
        except Exception as e:
            logger.error(f"Error fetching historical data for track {track_id}: {e}")
            self.db.rollback()
            track_stats["error"] = str(e)
        
        return track_stats
    
    def get_historical_data(
        self, 
        track_ids: List[str], 
        start_date: date, 
        end_date: date
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get historical data from the global cache for the given tracks and date range.
        
        Args:
            track_ids: List of Spotify track IDs
            start_date: Start date for historical data
            end_date: End date for historical data
            
        Returns:
            Dict mapping track_id to list of historical data entries
        """
        logger.info(f"Getting historical data from cache for {len(track_ids)} tracks from {start_date} to {end_date}")
        
        # Query all historical data for the tracks and date range
        entries = self.db.query(GlobalPlaycountHistory) \
            .filter(GlobalPlaycountHistory.spotify_track_id.in_(track_ids)) \
            .filter(GlobalPlaycountHistory.data_date >= start_date) \
            .filter(GlobalPlaycountHistory.data_date <= end_date) \
            .order_by(GlobalPlaycountHistory.spotify_track_id, GlobalPlaycountHistory.data_date) \
            .all()
        
        # Group by track_id
        result = {}
        for track_id in track_ids:
            result[track_id] = []
        
        for entry in entries:
            result[entry.spotify_track_id].append({
                'date': entry.data_date.isoformat(),
                'playcount': entry.playcount,
                'source': entry.source,
                'created_at': entry.created_at.isoformat() if entry.created_at else None
            })
        
        # Log statistics
        for track_id, data in result.items():
            if data:
                playcounts = [d['playcount'] for d in data]
                unique_playcounts = set(playcounts)
                logger.info(f"Track {track_id}: {len(data)} entries, {len(unique_playcounts)} unique playcounts")
            else:
                logger.warning(f"Track {track_id}: No cached data found")
        
        return result
    
    def clear_old_cache_entries(self, days_old: int = 365):
        """Clear cache entries older than specified days"""
        cutoff_date = date.today() - timedelta(days=days_old)
        
        old_entries = self.db.query(GlobalPlaycountHistory) \
            .filter(GlobalPlaycountHistory.data_date < cutoff_date) \
            .all()
        
        if old_entries:
            for entry in old_entries:
                self.db.delete(entry)
            
            self.db.commit()
            logger.info(f"Cleared {len(old_entries)} old cache entries older than {days_old} days")
        else:
            logger.info("No old cache entries to clear")
    
    def get_cache_statistics(self) -> Dict[str, Any]:
        """Get statistics about the global cache"""
        total_entries = self.db.query(GlobalPlaycountHistory).count()
        
        # Get date range of cached data
        min_date = self.db.query(GlobalPlaycountHistory.data_date).order_by(GlobalPlaycountHistory.data_date.asc()).first()
        max_date = self.db.query(GlobalPlaycountHistory.data_date).order_by(GlobalPlaycountHistory.data_date.desc()).first()
        
        # Get unique tracks
        unique_tracks = self.db.query(GlobalPlaycountHistory.spotify_track_id).distinct().count()
        
        return {
            "total_entries": total_entries,
            "unique_tracks": unique_tracks,
            "date_range": {
                "earliest": min_date[0].isoformat() if min_date else None,
                "latest": max_date[0].isoformat() if max_date else None
            }
        }
