import os
import sys
import csv
import time
import hdbscan
import pandas as pd
import geopandas as gpd
from itertools import product
from db_querier import DbQuerier
from collections import defaultdict
from shapely.geometry import LineString, Point

'''
!!! use anaconda env: geonenv on local machine (windows) - on cluster myenv contains all needed packages for all scripts!!!!
'''
# csv.field_size_limit(sys.maxsize)

# pyproj fix 'no database context specified'
# os.environ["PROJ_LIB"] = r"C:\Users\mhartman\Anaconda3\envs\transportation_Yolov5_env\Library\share\proj"

def str_polishing(text, special_chars):
    # remove curly brackets at this step
    polished_text = ''
    for char in text:
        if char not in special_chars:
            polished_text = polished_text + char
    # remove leading and trailing white space
    polished_text = polished_text.strip()
    return polished_text

def str_prefiltering(OCR_LOG_PATH, confidence_th = 0.0, min_str_length = 10, max_nr_special_chars=1):
    # special characters that are a sign of artificats during the ocr
    frame_dict = defaultdict(lambda: {'string_list': [], 'bbox_xmin': [], 'bbox_ymin': [], 'bbox_xmax': [], 'bbox_ymax': []})
    special_chars = "\"][()|{}_~€$!?%&+,;:~><*@¦=#^£\/´`'\''"
    # open and iterate over tracking log
    with open(OCR_LOG_PATH, 'r', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter=';', quoting=csv.QUOTE_NONE) #, quotechar='|'
        # skip header
        next(reader)
        for line in reader:
            frame_num = line[0]
            text = line[1]
            bbox_xmin = line[3]
            bbox_ymin = line[4]
            bbox_xmax = line[5]
            bbox_ymax = line[6]
            try:
                confidence = round(float(line[2]), 2)
            except Exception as e:
                pass
                # print(f'[!] Geoparsing Error: {e}')
            # filter steps on pure string
            if confidence >= confidence_th:
                count_special_chars = sum([1 for c in text if c in special_chars])
                if count_special_chars <= max_nr_special_chars:
                    polished_text = str_polishing(text, special_chars)
                    # check if only numbers
                    if not polished_text.isdigit():
                        try:
                            # print(f'{frame_num} - {confidence}: {polished_text}')
                            frame_dict[frame_num]['string_list'].append(polished_text)
                            frame_dict[frame_num]['bbox_xmin'].append(round(float(bbox_xmin)))
                            frame_dict[frame_num]['bbox_ymin'].append(round(float(bbox_ymin)))
                            frame_dict[frame_num]['bbox_xmax'].append(round(float(bbox_xmax)))
                            frame_dict[frame_num]['bbox_ymax'].append(round(float(bbox_ymax)))
                        except Exception as e:
                            print(f'[!] Geoparsing Error: {e}')
    # create independent copy of frame_dict
    frame_dict_complete = frame_dict
    # initialise HDBSCAN
    clusterer = hdbscan.HDBSCAN(min_cluster_size=2, min_samples=1)
    # keep single frame strings as well as a combination of the strings from the same frame
    # based on spatial closeness of strings (use center point of bbox for reference)
    for frame_nr, element in frame_dict.items():
        string_list = element['string_list']
        # build df based on bboxes in for the same video frame
        df = pd.DataFrame(list(zip(element['bbox_xmin'], element['bbox_ymin'], element['bbox_xmax'], element['bbox_ymax'])), columns=['bbox_xmin', 'bbox_ymin', 'bbox_xmax', 'bbox_ymax'])
        # check if lenght of df exceeds 1
        if len(df.index) > 1:
            # apply HDBSCAN to bbox coordinates to find spatially clustered strings detected by the OCR
            clusterer.fit(df)
            cluster_labels = clusterer.labels_
            # determine number of clusters
            unique_labels = set(cluster_labels)
            nr_clusters = len(unique_labels)
            if -1 in unique_labels:
                nr_clusters = nr_clusters - 1
            # add list to store concetenated strings to be added at the end
        else:
            cluster_labels = [0]
            nr_clusters = 1
        conc_strings = []
        # iterate over the labels and concatinate clustered strings based on their index in the labels list and their cluster nr
        for cluster_nr in range(nr_clusters):
            cluster_indexes = [index for index, label in enumerate(cluster_labels) if label == cluster_nr and label != -1]
            # get OCR strings in same cluster
            strings_in_cluster_original = [string_list[index] for index in cluster_indexes]
            strings_in_cluster = strings_in_cluster_original.copy()
            # add all possible combinations of strings inside the cluster
            for e1, e2 in product(strings_in_cluster, strings_in_cluster):
                if e1 != e2:
                    string_mutation = e1 + ' ' + e2
                    if string_mutation not in strings_in_cluster:
                        strings_in_cluster.append(e1 + ' ' + e2)
            # concatenate all strings in same cluster
            combined_string = ' '.join(strings_in_cluster_original)
            if combined_string not in strings_in_cluster:
                strings_in_cluster.append(combined_string)
            conc_strings += strings_in_cluster
        # additionally, add a combination of all strings from the same frame
        conc_strings.append(' '.join(string_list))
        string_list += conc_strings
        frame_dict_complete[frame_nr]['string_list'] = string_list
    # check minimum length of all final strings
    for frame_nr, element in frame_dict_complete.items():
        filtered_string_list = [string for string in element['string_list'] if len(string) >= min_str_length]
        frame_dict_complete[frame_nr]['string_list'] = filtered_string_list

    return frame_dict_complete

def trans_to_linestring(str_line):
    '''
    transform postgres geometry output which is a literal string into a stringly input for LineString object
    e.g. "LINESTRING(6.1467406 46.2011719,6.1465602 46.2010601)" -> LineString([[0, 0], [1, 0], [1, 1]])
    :return:
    '''
    point_string = str_line[11:-1].split(',')
    linestring = []
    try:
        for point_pair in point_string:
            point_x = point_pair.split(' ')[0].replace('(', '').replace(')', '')
            point_y = point_pair.split(' ')[1].replace('(', '').replace(')', '')
            linestring.append([float(point_x), float(point_y)])
        line_string = LineString(linestring)
    except Exception as e:
        print(f'[!] trans_to_linestring error: {e}')
        print(f'[!] point string causing error: {point_string}')
        line_string = LineString()
    return line_string


def geoparsing(frame_dict, OUTPUT_PATH, video_name):
    '''
    calculate Levenshtein Distance (word similarity) [https://towardsdatascience.com/calculating-string-similarity-in-python-276e18a7d33a]
    between the OCR detected words above a certain threshold and a gazetteer.
    This gazetteer can be narrowed down to a manually entered location e.g. Geneva
    '''
    # stores the df rows so that they can be added in one go -> better performance than df.append()
    df_row_storage = []
    # initialise dict that stores already geocoded and processed strings
    processed_dict = defaultdict(lambda: {'frame_nr': None, 'location_name': None, 'geo': None})
    # start db connection
    db_querier = DbQuerier()
    # count geolocations found
    geolocations_found = 0
    for frame_nr, element in frame_dict.items():
        geoparsing_str_list = element['string_list']
        for index, geoparsing_str in enumerate(geoparsing_str_list, 1):
            # check if string was geoparsed before, if yes retrieve previous result or skip
            if geoparsing_str in processed_dict:
                # append to pandas result df
                geolocations_found += 1
                df_row_storage.append(processed_dict[geoparsing_str])
                continue
            else:
                tries = 0
                while tries <= 3:
                    result = db_querier.levenshtein_dist_query(geoparsing_str)
                    # check result type
                    if result is not None:
                        for item in result:
                            geolocations_found += 1
                            # retrieve lat lng coordinates and name if there is a match
                            name = item[0]
                            geo = item[1]
                            processed_dict[geoparsing_str]['frame_nr'] = frame_nr
                            processed_dict[geoparsing_str]['location_name'] = name
                            processed_dict[geoparsing_str]['geo'] = trans_to_linestring(geo)
                            # append to result rows
                            df_row_storage.append(processed_dict[geoparsing_str])
                            # print(f'location name: {name}, geo: {geo}')
                        break
                    else:
                        tries += 1

    # check if there was anything returned
    if len(df_row_storage) != 0:
        # load geneva shapefile
        try:
            geneva_shp_df = gpd.read_postgis('SELECT * FROM geneva_poly', db_querier.conn, geom_col='st_polygonize')
        except Exception as e:
            print(f'[!] shp transaction error: {e} \n exiting.')
            exit()
        geneva_shp_df.to_crs("EPSG:3857", inplace=True)
        geneva_shp_df.rename_geometry('geometry', inplace=True)
        # add row_storage to df in one go
        df = gpd.GeoDataFrame(df_row_storage, geometry='geo', crs={'init': 'epsg:4326'}) # , crs="EPSG:4326"
        # reproject
        df.to_crs("EPSG:3857", inplace=True)
        # join both dfs
        # df = gpd.overlay(matchted_geoms_df, geneva_shp_df, how='union', keep_geom_type=False)
        df.drop_duplicates(inplace=True)
        # save geolocations to output file
        # define source string
        unique_location_names = df['location_name'].unique()
        GEOLOCATIONS_FILENAME = f'{time.strftime("%Y%m%d_%H%M%S")}_geolocations.csv'
        with open(os.path.join(OUTPUT_PATH, GEOLOCATIONS_FILENAME), 'wt', encoding='utf-8') as f:
            # header
            f.write('frame_nr;location_name;geo\n')
            for i_index, line in df.iterrows():
                frame_nr = line[0]
                location_name = line[1]
                geo = line[2]
                f.write(f'{frame_nr};{location_name};{geo}\n')
        print(f'\n[*] unique geolocation found: {len(unique_location_names)}')
        # pretty print df
        # with pd.option_context('display.max_rows', None, 'display.max_columns', None):
        #     print(df)
        # pickle dataframe
        pickle_filename = f"{time.strftime('%Y%m%d_%H%M%S')}_{video_name}_pickle.pkl"
        pickle_filepath = os.path.join(OUTPUT_PATH, pickle_filename)
        df.to_pickle(pickle_filepath)
        return df
    else:
        print(f'[!] geoparsing did not find any matches.')
        return None

if __name__ == '__main__':
    OCR_LOG_PATH = './output/Walking_in_GENEVA_4K_Switzerland_083545/20220222-083545_Walking_in_GENEVA_4K_Switzerland_083545_ocrlog.csv'
    frame_dict = str_prefiltering(OCR_LOG_PATH)
    df = geoparsing(frame_dict, OCR_LOG_PATH, './output/Walking_in_GENEVA_4K_Switzerland_083545')
