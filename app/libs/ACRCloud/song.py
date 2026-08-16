import json


class Song:
    def __init__(self, filename, file_id, tracks, loading):
        self.filename = filename
        self.file_id = file_id
        # eliminate duplicates
        self.tracks = list(set(tracks))
        self.loading = loading

    def __repr__(self):
        to_print = f"File {self.filename}"
        # bugged TODO
        # for track in self.tracks:
        #    to_print = to_print.join(f"\t{track}\n")
        return to_print

    def toJSON(self):
        return_val = {
            "filename": self.filename,
            "file_id": self.file_id,
            "tracks": [],
            "loading": self.loading,
        }
        for track in self.tracks:
            return_val["tracks"].append(track.toJSON())
        return return_val
