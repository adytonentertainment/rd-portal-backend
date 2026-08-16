Procedure:

* An user adds a song to the catalog.
* Stats endpoint is called and sees that no stats are available for this song.
* Fetch all data from songstats, keep last 30 days as daily entries and
aggregate everything below into weekly

* What to do if we have the next day and the next day is not available yet?
* Create a backgroundworker that gets triggered once a day, optimally when
songstats updates its data
* For the songs that have already been fetched, fetch for today and add it
to StatsCache
* Check if data can be aggregated and do it if it can

* What if a song has missing entries?
* Should not happen, add some way to restore data