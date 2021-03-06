import json
import time
from threading import Thread
from elasticsearch import Elasticsearch
import keys
from datasources import azlyrics
from datasources import genius
from datasources import spotify
from datasources import wikia
from processing import elasticsearchdb
from processing import processing

from datasources import billboards


class LyricScraper:

    def __init__(self, charts, start_date="2018-10-13",
                 stop_date='1958-01-01', backtrack=False,
                 es=False, max_records=20, max_threads=3):
        """

        :param charts:
        :param start_date:
        :param stop_date:
        :param backtrack:
        :param es:
        :param max_records:
        """
        self.start_date = start_date
        self.stop_date = stop_date
        self.backtrack = backtrack
        self.use_es = es
        self.charts = charts
        self.max_records = max_records
        self.max_threads = max_threads

        self.chart_partition = []
        for thread in range(self.max_threads):
            self.chart_partition.append([])
        for num in range(len(self.charts)):
            self.chart_partition[num%self.max_threads].\
                append(self.charts[num])
        print("Chart Partitions", self.chart_partition)

        api_keys = keys.Keys()
        self.AZ = azlyrics.AZLyricsScraper()
        self.GS = genius.GeniusScraper(
            token=api_keys.genius_token
        )
        self.SS = spotify.SpotifyScraper(
            client_id=api_keys.spotify_client_id,
            client_secret=api_keys.spotify_client_secret
        )
        self.BB = billboards.BillboardScraper()
        self.WS = wikia.WikiaScraper()
        self.Proc = processing.LyricAnalyst()
        if self.use_es:
            self.ES = elasticsearchdb.ElasticSearch("song_data")
        self.records_processed = 0

    def run(self):
        """
        Main run loop

        :return: None
        """
        begin = time.time()
        cur_date = self.start_date

        while (time.strptime(cur_date, "%Y-%m-%d") > time.strptime(self.stop_date, "%Y-%m-%d")) and \
                (self.max_records == 0 or self.records_processed < self.max_records):

            threads = []
            # Main process for data collection:
            for charts in self.chart_partition:
                t = Thread(target=self.get_data_load_balanced, args=(charts, cur_date), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

            # All charts are done for this time period. Cleanup:
            self.log_performance(begin)
            cur_date = self.BB.rewind_one_week(cur_date)

    def log_performance(self, begin):
        """
        Log usage report in file or in elasticsearch

        :param begin: timestamp from start of calling run()
        :return: None
        """
        if self.use_es:
            self.ES.log_usage(self.get_usage_reports(),
                              (time.time() - begin),
                              self.records_processed)
        else:
            self._log_to_file(self.get_usage_reports(),
                              "usage",
                              (str(self.records_processed) + "records"))
        self.clear_usage()

    def get_data_load_balanced(self, chart_list: list, date: str):
        """
        Wrapper for get_augemented_chart_list that cycles through a list of
        charts. This is called by each working thread, and allows a decoupling
        of number of threads to number of working charts.

        :param chart_list: list of charts to get data from (str)
        :param date: date of chart to read (str)
        :return:
        """
        for chart in chart_list:
            self.get_augmented_chart_list(chart=chart, date=date)

    def get_augmented_chart_list(self, chart: str, date: str):
        """
        Takes a list of billboard charts and gets
        all of the artist and track names from them, returns
        a dictionary of results

        :param chart: name of billboard chart (str)
        :param date: date to poll charts for
        :return: dictionary of chart info
        """
        master_dict = {}
        try:
            chart_dict = self.BB.get_chart(chart_name=chart, date_str=date)
        except:
            return {}

        for key, val in chart_dict.items():

            # Do we even need to do this?:
            if self.max_records != 0 and self.records_processed >= self.max_records:
                break
            status = "No Update"
            if key in ["Billboard_Chart", "Year", "Month", "Day"]:
                continue

            # setup main key and date array
            master_key = val["Artist"] + "_" + val["Title"]
            date_arr = {
                "Year": str(chart_dict["Year"]),
                "Month": str(chart_dict["Month"]),
                "Day": str(chart_dict["Day"])
            }

            # If new song:
            if (self.use_es and not self.ES.song_in_db(master_key)) or not self.use_es:
                song_dict = self._get_song_data(val, chart, date_arr, True)
                status = "New Entry"
                master_dict[master_key] = song_dict

            # Finished Message so we know there's progress
            print(self._progress_message(status, chart, date_arr,
                                         val, self.records_processed))
            self.records_processed += 1

        if self.use_es:
            self._put_data_in_es(master_dict)
        else:
            self._log_to_file(master_dict, chart, date)

    def _put_data_in_es(self, master_dict: dict):
        """
        puts augmented chart data into elasticsearch

        :param master_dict: dict of augmented chart/song data
        :return: none
        """
        for key, val in master_dict.items():
            if not self.ES.song_in_db(key):
                self.ES.put_new_data(song_data=val, unique_key=key)

    def _get_song_data(self, val: dict, chart: str, date_arr: dict,
                       flatten_lyrics=False) -> dict:
        """
        Gets data from all sources for a song

        :param val: dict containing song info
        :param chart: name of billboard chart (str)
        :param date_arr: array of date integers
        :param flatten_lyrics: boolean value to flatten lyrics
        :return: dict of aggregate data
        """
        song_dict = {}
        artist_name = val["Artist"]
        track_title = val["Title"]

        song_dict.update(  # Start with Billboard info
            self._setup_dict_for_new_key(val, chart, date_arr))
        song_dict.update(  # Add Genius Info
            self._get_genius_info(artist_name, track_title, flatten_lyrics))
        song_dict.update(  # Add Wikia Lyrics
            self._get_wikia_info(artist_name, track_title, flatten_lyrics))
        song_dict.update(  # Add AZ Lyrics
            self._get_az_info(artist_name, track_title, flatten_lyrics))
        song_dict.update(  # Add Spotify Metadata
            self._get_spotify_info(artist_name, track_title))
        # Add basic lyric analytics:
        results = self.Proc.get_lyric_stats([
            {"Genius": song_dict["Genius_Lyrics"]},
            {"AZ": song_dict["AZ_Lyrics"]},
            {"Wikia": song_dict["Wikia_Lyrics"]}
        ])
        song_dict.update(results)
        return song_dict

    @staticmethod
    def _setup_dict_for_new_key(val: dict, chart_name: str,
                                date_arr: dict) -> dict:
        """
        Setups up new dictionary with info about billboard chart of discovery

        :param date_arr: JSON dict of Year, Month, Date
        :param val: Dictionary from billboard module/api
        :param chart_name: Name of billboard chart discovered on
        :return: dict of form {
            "BB_Artist": "str",
            "BB_Featuring": "str",
            "BB_Song_Title": "str",
            "BB_Chart_Discovered": {
                "Chart_Name": "str",
                "Peak_Position": int
                "Date": "str"
            }
        }
        """
        return{"BB_Artist": val["Artist"],
               "BB_Featuring": val["Featuring"],
               "BB_Song_Title": val["Title"],
               "BB_Chart_Discovered":
                   {
                        "Chart_Name": chart_name,
                        "Peak_Position": val["Peak_Rank"],
                        "Date": date_arr["Year"] + '-' +
                                date_arr["Month"] + '-' +
                                date_arr["Day"]
                   }

               }

    def _get_az_info(self, artist_name: str, track_title: str,
                     flatten_lyrics=True):
        """
        Gets lyrics through Scraping AZLyrics for the given artist
        name and track title. Flatten is a boolean option that
        gets rid of capital letters, newlines, and punctuation.
        Empty string if lyrics aren't found

        :param artist_name: name of artist (str)
        :param track_title: name of track title (str)
        :param flatten_lyrics: flatten option as descirbed above (bool)
        :return: dict of form:
            {
                "AZ_Lyrics": "str",
                "AZ_Album": "str",
                "AZ_Written_By": ["str", "str"],
                "AZ_Year": "str",
                "AZ_Genre": "str",
            }
        """
        results = self.AZ.get_song_data(artist_name=artist_name,
                                        track_title=track_title,
                                        flatten_lyrics=flatten_lyrics)
        if results == {}:
            return {"AZ_Lyrics": ""}

        return {
            "AZ_Lyrics": results["lyrics"],
            "AZ_Album": results["album"],
            "AZ_Written_By": results["written by"],
            "AZ_Year": results["year"],
            "AZ_Genre": results["genre"]
        }

    def _get_genius_info(self, artist_name: str, track_title: str,
                         flatten_lyrics=True):
        """
        Gets lyrics through Genius API for the given artist
        name and track title. Flatten is a boolean option that
        gets rid of capital letters, newlines, and punctuation.
        Empty string if lyrics aren't found

        :param artist_name: name of artist (str)
        :param track_title: name of track (str)
        :param flatten_lyrics: flatten option (bool)
        :return: dict of form {"Genius_Lyrics": "str"}
        """
        results = self.GS.get_song_data(artist_name=artist_name,
                                        song_title=track_title,
                                        flatten_lyrics=flatten_lyrics)
        return {"Genius_Lyrics": results['lyrics']}

    def _get_spotify_info(self, artist_name: str, track_title: str):
        """
        Gets spotify metadata for a given artist name and track title.
        Searches spotify for the track and returns a data for an exact match.
        Returns the artist_id used be spotify (as it's used to find the genre),
        as well as the Genres (there can be many) the artist creates in, and
        some popularity statistics and release date for the track.

        :param artist_name: name of artist (str)
        :param track_title: name of track title (str)
        :return: dict of form {
            "Spotify_Artist_ID": "str",
            "Spotify_Release Date": "str",
            "Spotify_Song_Popularity": int,
            "Spotify_Genres": ["str", "str"],
            "Spotify_Artist_Followers": int
        }
        """
        results = self.SS.get_song_data(artist_name=artist_name,
                                        song_title=track_title)
        if results == {}:
            return {"Spotify_Artist_ID": ""}
        return {
            "Spotify_Artist_ID": results["Main_Artist_ID"],
            "Spotify_Release Date": results["Release_Date"],
            "Spotify_Song_Popularity": results["Song_Popularity"],
            "Spotify_Genres": results["Genres"],
            "Spotify_Artist_Followers": results["Artist_Followers"],
            "Spotify_Artist_Popularity": results["Artist_Popularity"]
        }

    def _get_wikia_info(self, artist_name: str, track_title: str,
                        flatten_lyrics=True):
        """
        Gets lyrics from wikia through PyLyrics Scraper

        :param artist_name: name of artist (str)
        :param track_title: name of track (str)
        :param flatten_lyrics: flatten option (bool)
        :return: dict of form {"Wikia_Lyrics": "str"}
        """
        return self.WS.get_lyrics(artist_name, track_title, flatten_lyrics)

    def get_usage_reports(self):
        """
        Creates aggregate usage report from all scraping/processing
        modules

        :return: dict
        """
        report_dict = {"Total_Records": self.records_processed}
        report_dict.update(self.BB.get_usage_report())
        report_dict.update(self.AZ.get_usage_report())
        report_dict.update(self.GS.get_usage_report())
        report_dict.update(self.WS.get_usage_report())
        report_dict.update(self.SS.get_usage_report())
        report_dict.update(self.Proc.get_usage_report())
        if self.use_es:
            report_dict.update(self.ES.get_usage_report())
        return report_dict

    def clear_usage(self):
        self.BB.clear_usage_stats()
        self.AZ.clear_usage_stats()
        self.GS.clear_usage_stats()
        self.WS.clear_usage_stats()
        self.SS.clear_usage_stats()
        self.Proc.clear_usage_stats()
        if self.use_es:
            self.ES.clear_usage_stats()

    @staticmethod
    def _log_to_file(data: dict, chart_name: str, date: str):
        """
        Logs augmented chart dict to file

        :param data: dictionary to log
        :param chart_name: name of chart (str)
        :param date: date string
        :return: None
        """
        file_path = 'sample_results/' + chart_name + '_'+date+'.json'
        with open(file_path, 'w') as outfile:
            json.dump(data, outfile, indent=4)

    @staticmethod
    def _progress_message(status: str, chart: str, date_arr: dict,
                          val: dict, records_processed: int) -> str:
        """
        Returns simple progress message so we know things haven't crashed

        :param status: status (str)
        :param chart: name of billboard chart (str)
        :param date_arr: dict of date values (integers) (dict)
        :param val: dict of song info
        :param records_processed: number of records processed (int)
        :return: string detailing progress
        """
        ret_str = status + " : " + chart + " : " + date_arr["Year"] + "-" + \
                  date_arr["Month"] + "-" + date_arr["Day"] + \
                  " : " + val["Artist"] + " : " + val["Title"] + \
                  " : " + "Records Processed: " + str(records_processed)
        return ret_str

if __name__ == "__main__":
    with open('run.json', 'r') as file:
        param = json.load(file)
    print("Running For Parameters:")
    charts = param["charts"]
    print("Charts :", charts)
    start_date = param["start_date"]
    print("Start Date : ", start_date)
    end_date = param["end_date"]
    print("End Date :", end_date)
    es = param["use_elastic_search"]
    print("Use ES? :", es)
    max_entries = param["max_entries"]
    print("Max Entries :", max_entries)
    max_threads = param["max_threads"]
    print("Max Threads :", max_threads)
    if es:
        time.sleep(10)
        Elasticsearch()
    LS = LyricScraper(charts=charts,
                      start_date=start_date,
                      stop_date=end_date,
                      backtrack=True,
                      es=es,
                      max_records=max_entries,
                      max_threads=max_threads)
    LS.run()
