import datetime
import json
import logging
import os
import re
import shutil

import cherrypy
import core
from core import plugins, snatcher, library

logging = logging.getLogger(__name__)


class Postprocessing(object):
    exposed = True

    def __init__(self):
        self.snatcher = snatcher.Snatcher()
        self.metadata = library.Metadata()

    def null(*args, **kwargs):
        return

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def POST(self, **data):
        ''' Handles post-processing requests.
        **data: keyword params send through POST request payload

        Required kw params:
            apikey (str): Watcher api key
            mode (str): post-processing mode (complete, failed)
            guid (str): download link of file. Can be url or magnet link.
            path (str): absolute path to downloaded files. Can be single file or dir

        Optional kw params:
            imdbid (str): imdb identification number (tt123456)
            downloadid (str): id number from downloader

        Returns dict of post-processing tasks and data
        '''

        logging.info('#################################')
        logging.info('Post-processing request received.')
        logging.info('#################################')

        # check for required keys
        for key in ('apikey', 'mode', 'guid', 'path'):
            if key not in data:
                logging.warning('Missing key {}'.format(key))
                return {'response': False, 'error': 'missing key: {}'.format(key)}

        # check if api key is correct
        if data['apikey'] != core.CONFIG['Server']['apikey']:
            logging.warning('Incorrect API key.'.format(key))
            return {'response': False, 'error': 'incorrect api key'}

        # check if mode is valid
        if data['mode'] not in ('failed', 'complete'):
            logging.warning('Invalid mode value: {}.'.format(data['mode']))
            return {'response': False, 'error': 'invalid mode value'}

        logging.debug(data)

        # modify path based on remote mapping
        data['path'] = self.map_remote(data['path'])

        # get the actual movie file name
        data['movie_file'] = self.get_movie_file(data['path'])

        # Get possible local data or get TMDB data to merge with self.params.
        logging.info('Gathering release information.')
        data.update(self.get_movie_info(data))

        # At this point we have all of the information we're going to get.
        if data['mode'] == 'failed':
            logging.warning('Post-processing as Failed.')
            response = self.failed(data)
            logging.warning(response)
        elif data['mode'] == 'complete':
            logging.info('Post-processing as Complete.')

            response = self.complete(data)

            response['data'].pop('backlog', '')
            response['data'].pop('predb', '')
            response['data'].pop('source', '')

            title = response['data'].get('title')
            year = response['data'].get('year')
            imdbid = response['data'].get('imdbid')
            resolution = response['data'].get('resolution')
            rated = response['data'].get('rated')
            original_file = response['data'].get('original_file')
            finished_file = response['data'].get('finished_file')
            downloadid = response['data'].get('downloadid')
            finished_date = response['data'].get('finished_date')
            quality = response['data'].get('quality')

            plugins.finished(title, year, imdbid, resolution, rated, original_file, finished_file, downloadid, finished_date, quality)

            logging.info(response)
        else:
            logging.warning('Invalid mode value: {}.'.format(data['mode']))
            return {'response': False, 'error': 'invalid mode value'}

        logging.info('#################################')
        logging.info('Post-processing complete.')
        logging.info(json.dumps(response, indent=2, sort_keys=True))
        logging.info('#################################')

        return response

    def get_movie_file(self, path):
        ''' Looks for the filename of the movie being processed
        path (str): url-passed path to download dir

        If path is a file, just returns path.
        If path is a directory, recursively finds the largest file in that dir.

        Returns str absolute path of movie file
        '''

        logging.info('Finding movie file.')
        if os.path.isfile(path):
            logging.info('Post-processing file {}.'.format(path))
            return path
        else:
            # Find the biggest file in the dir. Assume that this is the movie.
            try:
                files = os.listdir(path)
            except Exception as e:
                logging.error('Path not found in filesystem. Will be unable to move or rename.',
                              exc_info=True)
                return ''

            files = []
            for root, dirs, filenames in os.walk(path):
                for file in filenames:
                    files.append(os.path.join(root, file))

            if files == []:
                return ''

            biggestfile = None
            s = 0
            for file in files:
                size = os.path.getsize(file)
                if size > s:
                    biggestfile = file
                    s = size

            logging.info('Post-processing file {}.'.format(biggestfile))

            return biggestfile

    def get_movie_info(self, data):
        ''' Gets score, imdbid, and other information to help process
        data (dict): url-passed params with any additional info

        Uses guid to look up local details.
        If that fails, uses downloadid.
        If that fails, searches tmdb for imdbid

        If everything fails returns empty dict {}

        Returns dict of any gathered information
        '''

        # try to get searchresult using guid first then downloadid
        logging.info('Searching local database for guid.')
        result = core.sql.get_single_search_result('guid', data['guid'])
        if not result:
            logging.info('Guid not found.')
            if data.get('downloadid'):
                logging.info('Searching local database for downloadid.')
                result = core.sql.get_single_search_result('downloadid', str(data['downloadid']))
                if result:
                    logging.info('Searchresult found by downloadid.')
                    if result['guid'] != data['guid']:
                        logging.info('Guid for downloadid does not match local data. '
                                     'Adding guid2 to processing data.')
                        data['guid2'] = result['guid']
                else:
                    logging.info('Unable to find search result with downloadid {}'.format(data['downloadid']))
        else:
            logging.info('Searchresult found by guid.')

        # if we found it, get local movie info
        if result:
            logging.info('Searching local database by imdbid.')
            local = core.sql.get_movie_details('imdbid', result['imdbid'])
            if local:
                logging.info('Movie data found locally by imdbid.')
                data.update(local)
                data['finished_score'] = result['score']
                data['resolution'] = result['resolution']
                data['downloadid'] = result['downloadid']
            else:
                logging.info('Unable to find movie in local db.')

        # Still no luck? Try to get the info from TMDB
        else:
            logging.info('Unable to find local data for release. Using only data found from file.')

        if data:
            mdata = self.metadata.from_file(data['movie_file'])
            mdata.update(data)
            if not mdata.get('quality'):
                data['quality'] = 'Default'
            return mdata
        else:
            return {}

    def failed(self, data):
        ''' Post-process a failed download
        data (dict): of gathered data from downloader and localdb/tmdb

        In SEARCHRESULTS marks guid as Bad
        In MARKEDRESULTS:
            Creates or updates entry for guid and optional guid2 with status=Bad
        Updates MOVIES status

        If Clean Up is enabled will delete path and contents.
        If Auto Grab is enabled will grab next best release.

        Returns dict of post-processing results
        '''

        config = core.CONFIG['Postprocessing']

        # dict we will json.dump and send back to downloader
        result = {}
        result['status'] = 'finished'
        result['data'] = data
        result['tasks'] = {}

        # mark guid in both results tables
        logging.info('Marking guid as Bad.')
        guid_result = {'url': data['guid']}

        if data['guid']:  # guid can be empty string
            if core.manage.searchresults(data['guid'], 'Bad'):
                guid_result['update_SEARCHRESULTS'] = True
            else:
                guid_result['update_SEARCHRESULTS'] = False

            if core.manage.markedresults(data['guid'], 'Bad', imdbid=data['imdbid']):
                guid_result['update_MARKEDRESULTS'] = True
            else:
                guid_result['update_MARKEDRESULTS'] = False

        # create result entry for guid
        result['tasks']['guid'] = guid_result

        # if we have a guid2, do it all again
        if 'guid2' in data.keys():
            logging.info('Marking guid2 as Bad.')
            guid2_result = {'url': data['guid2']}
            if core.manage.searchresults(data['guid2'], 'Bad'):
                guid2_result['update SEARCHRESULTS'] = True
            else:
                guid2_result['update SEARCHRESULTS'] = False

            if core.manage.markedresults(data['guid2'], 'Bad', imdbid=data['imdbid'], ):
                guid2_result['update_MARKEDRESULTS'] = True
            else:
                guid2_result['update_MARKEDRESULTS'] = False
            # create result entry for guid2
            result['tasks']['guid2'] = guid2_result

        # set movie status
        if data['imdbid']:
            logging.info('Setting MOVIE status.')
            r = core.manage.movie_status(data['imdbid'])
        else:
            logging.info('Imdbid not supplied or found, unable to update Movie status.')
            r = ''
        result['tasks']['update_movie_status'] = r

        # delete failed files
        if config['cleanupfailed']:
            result['tasks']['cleanup'] = {'enabled': True, 'path': data['path']}

            logging.info('Deleting leftover files from failed download.')
            if self.cleanup(data['path']) is True:
                result['tasks']['cleanup']['response'] = True
            else:
                result['tasks']['cleanup']['response'] = False
        else:
            result['tasks']['cleanup'] = {'enabled': False}

        # grab the next best release
        if core.CONFIG['Search']['autograb']:
            result['tasks']['autograb'] = {'enabled': True}
            logging.info('Grabbing the next best release.')
            if data.get('imdbid') and data.get('quality'):
                best_release = self.snatcher.best_release(data)
                if best_release and self.snatcher.download(best_release):
                    r = True
                else:
                    r = False
            else:
                r = False
            result['tasks']['autograb']['response'] = r
        else:
            result['tasks']['autograb'] = {'enabled': False}

        # all done!
        result['status'] = 'finished'
        return result

    def complete(self, data):
        ''' Post-processes a complete, successful download
        data (dict): all gathered file information and metadata

        data must include the following keys:
            path (str): path to downloaded item. Can be file or directory
            guid (str): nzb guid or torrent hash
            downloadid (str): download id from download client

        All params can be empty strings if unknown

        In SEARCHRESULTS marks guid as Finished
        In MARKEDRESULTS:
            Creates or updates entry for guid and optional guid with status=bad
        In MOVIES updates finished_score and finished_date
        Updates MOVIES status

        Checks to see if we found a movie file. If not, ends here.

        If Renamer is enabled, renames movie file according to core.CONFIG
        If Mover is enabled, moves file to location in core.CONFIG, then...
            If Clean Up enabled, deletes path after Mover finishes.
            Clean Up will not execute without Mover success.

        Returns dict of post-processing results
        '''
        config = core.CONFIG['Postprocessing']

        # dict we will json.dump and send back to downloader
        result = {}
        result['status'] = 'incomplete'
        result['data'] = data
        result['data']['finished_date'] = str(datetime.date.today())
        result['tasks'] = {}

        # mark guid in both results tables
        logging.info('Marking guid as Finished.')
        guid_result = {}
        if data['guid'] and data.get('imdbid'):
            if core.manage.searchresults(data['guid'], 'Finished', movie_info=data):
                guid_result['update_SEARCHRESULTS'] = True
            else:
                guid_result['update_SEARCHRESULTS'] = False

            if core.manage.markedresults(data['guid'], 'Finished', imdbid=data['imdbid']):
                guid_result['update_MARKEDRESULTS'] = True
            else:
                guid_result['update_MARKEDRESULTS'] = False

            # create result entry for guid
            result['tasks'][data['guid']] = guid_result

        # if we have a guid2, do it all again
        if data.get('guid2') and data.get('imdbid'):
            logging.info('Marking guid2 as Finished.')
            guid2_result = {}
            if core.manage.searchresults(data['guid2'], 'Finished', movie_info=data):
                guid2_result['update_SEARCHRESULTS'] = True
            else:
                guid2_result['update_SEARCHRESULTS'] = False

            if core.manage.markedresults(data['guid2'], 'Finished', imdbid=data['imdbid'],
                                         ):
                guid2_result['update_MARKEDRESULTS'] = True
            else:
                guid2_result['update_MARKEDRESULTS'] = False

            # create result entry for guid2
            result['tasks'][data['guid2']] = guid2_result

        # set movie status and add finished date/score
        if data.get('imdbid'):
            if not core.sql.row_exists('MOVIES', imdbid=data['imdbid']):
                logging.info('{} not found in library, adding now.'.format(data.get('title')))
                data['status'] = 'Disabled'
                core.manage.add_movie(data)

            logging.info('Setting MOVIE status.')
            r = core.manage.movie_status(data['imdbid'])
            db_update = {'finished_date': result['data']['finished_date'], 'finished_score': result['data'].get('finished_score')}
            core.sql.update_multiple('MOVIES', db_update, imdbid=data['imdbid'])

        else:
            logging.info('Imdbid not supplied or found, unable to update Movie status.')
            r = ''
        result['tasks']['update_movie_status'] = r

        data.update(self.metadata.convert_to_db(data))
        result['data']['original_file'] = result['data']['movie_file']

        # renamer
        if config['renamerenabled']:
            result['tasks']['renamer'] = {'enabled': True}
            new_file_name = self.renamer(data)
            if new_file_name == '':
                result['tasks']['renamer']['response'] = False
            else:
                path = os.path.split(data['movie_file'])[0]
                data['movie_file'] = os.path.join(path, new_file_name)
                result['tasks']['renamer']['response'] = True
        else:
            logging.info('Renamer disabled.')
            result['tasks']['mover'] = {'enabled': False}

        # mover
        if config['moverenabled']:
            result['tasks']['mover'] = {'enabled': True}
            response = self.mover(data)
            if not response:
                result['tasks']['mover']['response'] = False
            else:
                data['finished_file'] = response
                result['tasks']['mover']['response'] = True
        else:
            logging.info('Mover disabled.')
            result['tasks']['mover'] = {'enabled': False}

        if data.get('imdbid'):
            core.sql.update('MOVIES', 'finished_file', result['data'].get('finished_file'), 'imdbid', data['imdbid'])

        # Delete leftover dir. Skip if file links are enabled or if mover disabled/failed
        if config['cleanupenabled']:
            result['tasks']['cleanup'] = {'enabled': True}

            if config['movermethod'] in ('hardlink', 'symboliclink'):
                logging.info('File linking enabled -- skipping Cleanup.')
                result['tasks']['cleanup']['response'] = None
                return result

            elif os.path.isfile(data['path']):
                logging.info('Download is file, not directory -- skipping Cleanup.')
                result['tasks']['cleanup']['response'] = None
                return result

            # fail if mover disabled or failed
            if config['moverenabled'] is False or result['tasks']['mover']['response'] is False:
                logging.info('Mover either disabled or failed -- skipping Cleanup.')
                result['tasks']['cleanup']['response'] = None
            else:
                if self.cleanup(data['path']):
                    r = True
                else:
                    r = False
                result['tasks']['cleanup']['response'] = r
        else:
            result['tasks']['cleanup'] = {'enabled': False}

        # all done!
        result['status'] = 'finished'
        return result

    def map_remote(self, path):
        ''' Alters directory based on remote mappings settings
        path (str): path from download client

        Replaces the base of the file tree with the 'local' mapping.
            Ie, '/home/user/downloads/Watcher' becomes '//server/downloads/Watcher'

        'path' can be file or directory, it doesn't matter.

        If more than one match is found, defaults to the longest path.
            remote: local = '/home/users/downloads/': '//server/downloads/'
                            '/home/users/downloads/Watcher/': '//server/downloads/Watcher/'
            In this case, a supplied remote '/home/users/downloads/Watcher/' will match a
            startswith() for both supplied settings. So we will default to the longest path.

        Returns str new path
        '''

        maps = core.CONFIG['Postprocessing']['RemoteMapping']

        matches = []
        for remote in maps.keys():
            if path.startswith(remote):
                matches.append(remote)
        if not matches:
            return path
        else:
            match = max(matches, key=len)
            new_path = path.replace(match, maps[match])
            logging.info('Changing remote path from {} to {}'.format(path, new_path))
            return new_path

    def compile_path(self, string, data):
        ''' Compiles string to file/path names
        string (str): brace-formatted string to substitue values (ie '/movies/{title}/')
        data (dict): of values to sub into string

        Takes a renamer/mover path and adds values.
            ie '{title} {year} {resolution}' -> 'Movie 2017 1080P'
        Subs double spaces. Trims trailing spaces. Removes any invalid characters.

        Can return blank string ''

        Sends string to self.sanitize() to remove illegal characters

        Returns str new path
        '''
        new_string = string
        for k, v in data.items():
            k = "{" + k + "}"
            if k in new_string:
                new_string = new_string.replace(k, (v or ''))

        while '  ' in new_string:
            new_string = new_string.replace('  ', ' ')

        while len(new_string) > 1 and new_string[-1] == ' ':
            new_string = new_string[:-1]

        new_string = self.map_remote(new_string)

        logging.debug('Created path {} from {}'.format(new_string, string))

        return self.sanitize(new_string)

    def renamer(self, data):
        ''' Renames movie file based on renamerstring.
        data (dict): movie information.

        Renames movie file based on params in core.CONFIG

        Returns str new file name (blank string on failure)
        '''

        config = core.CONFIG['Postprocessing']

        renamer_string = config['renamerstring']

        # check to see if we have a valid renamerstring
        if re.match(r'{(.*?)}', renamer_string) is None:
            logging.info('Invalid renamer string {}'.format(renamer_string))
            return ''

        # existing absolute path
        abs_path_old = data['movie_file']
        file_path = os.path.split(data['movie_file'])[0]

        # get the extension
        ext = os.path.splitext(abs_path_old)[1]

        # get the new file name
        new_name = self.compile_path(renamer_string, data)

        if not new_name:
            logging.info('New file name would be blank. Cancelling renamer.')
            return ''

        if core.CONFIG['Postprocessing']['replacespaces']:
            new_name = new_name.replace(' ', '.')
        new_name = new_name + ext

        # new absolute path
        abs_path_new = os.path.join(file_path, new_name)

        logging.info('Renaming {} to {}'.format(os.path.basename(data['movie_file']), new_name))
        try:
            os.rename(abs_path_old, abs_path_new)
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as e:
            logging.error('Renamer failed: Could not rename file.', exc_info=True)
            return ''

        # return the new name so the mover knows what our file is
        return new_name

    def recycle(self, recycle_bin, abs_filepath):
        ''' Sends file to recycle bin dir
        recycle_bin (str): absolute path to recycle bin directory
        abs_filepath (str): absolute path of file to recycle

        Creates recycle_bin dir if neccesary.
        Moves file to recycle bin. If a file with the same name already
            exists, overwrites existing file.

        Returns bool
        '''

        file_dir, file_name = os.path.split(abs_filepath)
        if not os.path.isdir(recycle_bin):
            logging.info('Creating recycle bin directory {}'.format(recycle_bin))
            try:
                os.makedirs(recycle_bin)
            except Exception as e:
                logging.error('Recycling failed: Could not create Recycle Bin directory {}.'.format(recycle_bin), exc_info=True)
                return False
        logging.info('Recycling {} to recycle bin {}'.format(abs_filepath, recycle_bin))
        try:
            if os.path.isfile(os.path.join(recycle_bin, file_name)):
                os.remove(os.path.join(recycle_bin, file_name))
            shutil.copystat = self.null
            shutil.move(abs_filepath, recycle_bin)
            return True
        except Exception as e:
            logging.error('Recycling failed: Could not move file.', exc_info=True)
            return False

    def remove_additional_files(self, movie_file):
        ''' Removes addtional associated file of movie_file
        movie_file (str): absolute file path of old movie file

        Removes any file in movie_file's directory that share the same file name

        Does not cause mover failure on error.

        Returns bool
        '''

        logging.info('Removing additional files for {}'.format(movie_file))

        path, file_name = os.path.split(movie_file)

        fname = os.path.splitext(file_name)[0]

        for i in os.listdir(path):
            if os.path.splitext(i)[0] == fname:
                logging.info('Removing additional file {}'.format(i))
                try:
                    os.remove(os.path.join(path, i))
                except Exception as e:
                    logging.warning('Unable to remove {}'.format(i), exc_info=True)
                    return False
        return True

    def mover(self, data):
        '''Moves movie file to path constructed by moverstring
        data (dict): movie information.

        Moves file to location specified in core.CONFIG

        If target file already exists either:
            Delete it prior to copying new file in (since os.rename in windows doesn't overwrite)
                OR:
            Create Recycle Bin directory (if neccesary) and move the old file there.

        Copies and renames additional files

        Returns str new file location (blank string on failure)
        '''

        config = core.CONFIG['Postprocessing']
        if config['recyclebinenabled']:
            recycle_bin = self.compile_path(config['recyclebindirectory'], data)
        target_folder = os.path.normpath(self.compile_path(config['moverpath'], data))
        target_folder = os.path.join(target_folder, '')

        # if the new folder doesn't exist, make it
        try:
            if not os.path.exists(target_folder):
                os.makedirs(target_folder)
        except Exception as e:
            logging.error('Mover failed: Could not create directory {}.'.format(target_folder), exc_info=True)
            return ''

        current_file_path = data['movie_file']
        current_path, file_name = os.path.split(current_file_path)
        # If finished_file exists, recycle or remove
        if data.get('finished_file'):
            old_movie = data['finished_file']
            logging.info('Checking if old file {} exists.'.format(old_movie))
            if os.path.isfile(old_movie):

                if config['recyclebinenabled']:
                    logging.info('Old movie file found, recycling.')
                    if not self.recycle(recycle_bin, old_movie):
                        return ''
                else:
                    logging.info('Deleting old file {}'.format(old_movie))
                    try:
                        os.remove(old_movie)
                    except Exception as e:
                        logging.error('Mover failed: Could not delete file.', exc_info=True)
                        return ''
                if config['removeadditionalfiles']:
                    self.remove_additional_files(old_movie)
        # Check if the target file name exists in target dir, recycle or remove
        if os.path.isfile(os.path.join(target_folder, file_name)):
            existing_movie_file = os.path.join(target_folder, file_name)
            logging.info('Existing file {} found in {}'.format(file_name, target_folder))
            if config['recyclebinenabled']:
                if not self.recycle(recycle_bin, existing_movie_file):
                    return ''
            else:
                logging.info('Deleting old file {}'.format(existing_movie_file))
                try:
                    os.remove(existing_movie_file)
                except Exception as e:
                    logging.error('Mover failed: Could not delete file.', exc_info=True)
                    return ''
            if config['removeadditionalfiles']:
                self.remove_additional_files(existing_movie_file)

        # Finally the actual move process
        new_file_location = os.path.join(target_folder, os.path.basename(data['movie_file']))

        if config['movermethod'] == 'hardlink':
            logging.info('Creating hardlink from {} to {}.'.format(data['original_file'], new_file_location))
            try:
                os.link(data['original_file'], new_file_location)
            except Exception as e:
                logging.error('Mover failed: Unable to create hardlink.', exc_info=True)
                return ''
        else:
            logging.info('Moving {} to {}'.format(current_file_path, target_folder))
            try:
                shutil.copystat = self.null
                shutil.move(current_file_path, target_folder)
            except Exception as e:
                logging.error('Mover failed: Could not move file.', exc_info=True)
                return ''

            if config['movermethod'] == 'symboliclink':
                if core.PLATFORM == 'windows':
                    logging.warning('Attempting to create symbolic link on Windows. This will fail without SeCreateSymbolicLinkPrivilege.')
                logging.info('Creating symbolic link from {} to {}'.format(new_file_location, data['original_file']))
                try:
                    os.symlink(new_file_location, data['original_file'])
                except Exception as e:
                    logging.error('Mover failed: Unable to create symbolic link.', exc_info=True)
                    return ''

        keep_extensions = [i for i in config['moveextensions'].split(',') if i != '']

        if len(keep_extensions) > 0:
            logging.info('Moving addition files with extensions {}.'.format(','.join(keep_extensions)))
            renamer_string = config['renamerstring']
            new_name = self.compile_path(renamer_string, data)

            for root, dirs, filenames in os.walk(data['path']):
                for name in filenames:
                    old_abs_path = os.path.join(root, name)
                    ext = os.path.splitext(old_abs_path)[1]  # '.ext'

                    target_file = '{}{}'.format(os.path.join(target_folder, new_name), ext)

                    if ext.replace('.', '') in keep_extensions:
                        append = 0
                        while os.path.isfile(target_file):
                            append += 1
                            new_filename = '{}({})'.format(new_name, str(append))
                            target_file = '{}{}'.format(os.path.join(target_folder, new_filename), ext)
                        try:
                            logging.info('Moving {} to {}'.format(old_abs_path, target_file))
                            shutil.copyfile(old_abs_path, target_file)
                        except Exception as e:
                            logging.error('Moving additional files failed: Could not copy {}.'.format(old_abs_path), exc_info=True)
        return new_file_location

    def cleanup(self, path):
        ''' Deletes specified path
        path (str): of path to remover

        path can be file or dir

        Returns bool
        '''

        # if its a dir
        if os.path.isdir(path):
            try:
                shutil.rmtree(path)
                return True
            except Exception as e:
                logging.error('Could not delete path.', exc_info=True)
                return False
        elif os.path.isfile(path):
            # if its a file
            try:
                os.remove(path)
                return True
            except Exception as e:
                logging.error('Could not delete path.', exc_info=True)
                return False
        else:
            # if it is somehow neither
            return False

    def sanitize(self, string):
        ''' Sanitize file names and paths
        string (str): to sanitize

        Removes all illegal characters or replaces them based on
            user's config.

        Returns str
        '''
        config = core.CONFIG['Postprocessing']
        repl = config['replaceillegal']

        string = re.sub(r'["*?<>|]+', repl, string)

        drive, path = os.path.splitdrive(string)
        path = path.replace(':', repl)
        return ''.join([drive, path])
