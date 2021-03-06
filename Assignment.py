__author__ = 'Nitin Pasumarthy'

import psycopg2
from itertools import islice
import math
import os
from multiprocessing.pool import ThreadPool
import thread

import RatingsDAO
import Globals
import MetaDataDAO


MAX_LINES_COUNT_READ = 100000  # Maximum number of lines to read into memory.
LINE_SIZE = 21  # Length of each line in bytes to quickly calculate the percent of file read
DATABASE_NAME = 'dds_assgn1'
MAX_RATING = 5.0
RANGE_PARTITION_TABLE_PREFIX = 'range_part'
RROBIN_PARTITION_TABLE_PREFIX = 'rrobin_part'


def getnextchunk(filepath):
    """
    Reads files in chunks in an efficient manner using isslice method.
    Uses yield to return the next chunk if an existing file is being read
    :param filepath: relative or abs path of the file to read
    :return:Chunk of lines using yield
    """
    abs_filepath = os.path.abspath(filepath)
    print(abs_filepath)
    with open(abs_filepath) as f:
        linesinfile = os.path.getsize(abs_filepath) / LINE_SIZE
        totallinesread = 0.0
        while True:
            lines = list(islice(f, MAX_LINES_COUNT_READ))
            linesread = len(lines)
            totallinesread += linesread
            if Globals.DEBUG:  Globals.printinfo(
                'Read {0} lines of {1}: {2}% complete approximately'.format(totallinesread, linesinfile,
                                                                            totallinesread / linesinfile * 100))
            yield lines
            if linesread < MAX_LINES_COUNT_READ:
                break


def getopenconnection(user='postgres', password='1234', dbname='postgres'):
    """
    Connects to dds_assgn1 database using postgres user
    :return: Open DB connection
    """
    return psycopg2.connect("dbname='" + dbname + "' user='" + user + "' host='localhost' password='" + password + "'")


def create_db(dbname):
    """
    We create a DB by connecting to the default user and database of Postgres
    The function first checks if an existing database exists for a given name, else creates it.
    :return:None
    """
    # Connect to the default database
    con = getopenconnection()
    con.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = con.cursor()

    # Check if an existing database with the same name exists
    cur.execute('SELECT COUNT(*) FROM pg_catalog.pg_database WHERE datname=\'%s\'' % (dbname,))
    count = cur.fetchone()[0]
    if count == 0:
        cur.execute('CREATE DATABASE %s' % (dbname,))  # Create the database
    else:
        Globals.printinfo('A database named "{0}" already exists'.format(dbname))

    # Clean up
    cur.close()
    con.close()


def loadratings(ratingstablename, ratingsfilepath, openconnection):
    """
    Loads the file into DB
    :param ratingsfilepath: relative or abs path of the file to load
    :param openconnection: open connection to DB
    :return: None
    """
    RatingsDAO.create(openconnection, ratingstablename)
    count = 0
    for lines in getnextchunk(ratingsfilepath):
        ratings = []
        for line in lines:
            rating = line.split('::')[0:3]
            ratings.append(rating)
            count += 1
        RatingsDAO.insert(ratings, openconnection, ratingstablename)
    Globals.printinfo("Loaded {0} ratings into DB".format(count))


def rangepartition(ratingstablename, numberofpartitions, openconnection):
    """
    Partitions the ratings table in to the given number of partition using Range based partitioning scheme
    Partitioned table names will be starting from 1. If the number of partitions are N, the range of Rating values,
    0 to MAX_RATING, will be split uniformly into N pieces.
    Eg: N = 5
    Partition 1: all movies with a rating => [0, 1]
    Partition 2: all movies with a rating => (1, 2]
    Partition 3: all movies with a rating => (2, 3]
    Partition 4: all movies with a rating => (3, 4]
    Partition 5: all movies with a rating => (4, 5]
    As shown, movies with zero rating will be placed in the first partition
    :param numberofpartitions: Number of partitions
    :param openconnection: open connection to DB
    :return:None
    """
    if numberofpartitions <= 0 or not isinstance(numberofpartitions, int): raise AttributeError(
        "Number of partitions should be a positive integer")

    inc = round(float(MAX_RATING) / numberofpartitions, 10)  # precision restricted to 10 decimal places
    lower_bound = 0.0
    upper_bound = lower_bound + inc

    sno = 1
    while upper_bound <= MAX_RATING:
        createrangepartitionandinsert(openconnection, lower_bound, sno, upper_bound, ratingstablename)
        lower_bound += inc
        upper_bound += inc
        sno += 1

    # If number of partitions is not divisible by MAX_RATING, the last partition will be missed due to rounding
    if lower_bound != MAX_RATING:
        createrangepartitionandinsert(openconnection, lower_bound, sno, MAX_RATING, ratingstablename)

    # save the movies with zero rating in the first partition
    createrangepartitionandinsert(openconnection, -1, 1, 0, ratingstablename, False)

    # save the number of partitions in the meta data table
    MetaDataDAO.create(openconnection)  # Create if the table doesnt exist
    MetaDataDAO.upsert(openconnection, Globals.RANGE_PARTITIONS_KEY, numberofpartitions)


def roundrobinpartition(ratingstablename, numberofpartitions, openconnection):
    """
    Partition the ratings table into 'numberofpartitions' pieces in a round robin manner
    Partitions will be zero indexed.
    Eg: N = 3 partitions
    Partition 0: IDs = [0,3,6..]
    Partition 1: IDs = [1,3,7...]
    Partition 2: IDs = [2,4,8...]
    :param numberofpartitions: Number of partitions
    :param openconnection: open connection to DB
    :return:None
    """
    if numberofpartitions <= 0 or not isinstance(numberofpartitions, int): raise AttributeError(
        "Number of partitions should be a positive integer")

    numberofratings = RatingsDAO.numberofratings(openconnection, ratingstablename)
    # Assumption: IDs are in order from 1 to total number of ratings in Ratings table
    allids = range(1, numberofratings + 1)
    for i in range(0, numberofpartitions):
        ratingids = filter(lambda x: x % numberofpartitions == i, allids)
        createrobinpartitionandinsert(openconnection, i, ratingids, ratingstablename)

    # save the number of partitions in the meta data table
    MetaDataDAO.create(openconnection)  # Create if the table doesnt exist
    MetaDataDAO.upsert(openconnection, Globals.RROBIN_PARTITIONS_KEY, numberofpartitions)


def roundrobininsert(ratingstablename, userid, itemid, rating, openconnection):
    """
    Insert a new rating into round robin based partitioned tables
    An error is thrown if this method is called without creating range based pratition tables
    :param openconnection: open connection to DB
    :param userid: 1st column of ratings table, User ID
    :param itemid: 2nd column of ratings table, Movie ID
    :param rating: 3rd column of ratings table, Rating
    :return:None
    """
    if not validaterating(rating): return
    n = MetaDataDAO.select(openconnection, Globals.RROBIN_PARTITIONS_KEY)
    if n is None:
        Globals.printwarning("First create the partitions and then try to insert")
        return
    n = int(n)

    numberofratings = RatingsDAO.numberofratings(openconnection, ratingstablename)
    partitionindex = (numberofratings + 1) % n
    destinationtable = RROBIN_PARTITION_TABLE_PREFIX + str(partitionindex)
    RatingsDAO.insert([(userid, itemid, rating)], openconnection, destinationtable)
    # also insert into the ratings table as we are using computing partition index based on number of
    # rows in ratings table above
    RatingsDAO.insert([(userid, itemid, rating)], openconnection, ratingstablename)
    if Globals.DEBUG: Globals.printinfo(
        'Inserted rating (UserID: {0}, MovieID: {1}, Rating: {2}), to "{3}" table'.format(userid, itemid, rating,
                                                                                          destinationtable))


def rangeinsert(ratingstablename, userid, itemid, rating, openconnection):
    """
    Insert a new rating into range based partitioned tables
    An error is thrown if this method is called without creating range based pratition tables
    :param openconnection: open connection to DB
    :param userid: 1st column of ratings table, User ID
    :param itemid: 2nd column of ratings table, Movie ID
    :param rating: 3rd column of ratings table, Rating
    :return:None
    """
    if not validaterating(rating): return
    n = MetaDataDAO.select(openconnection, Globals.RANGE_PARTITIONS_KEY)
    if n is None:
        Globals.printwarning("First create the partitions and then try to insert")
        return
    n = int(n)

    partitionwidth = float(MAX_RATING) / n
    # to handle cases when rating is 0, max function is used. Will be inserted in first patition
    partitionindex = max(int(math.ceil(rating / partitionwidth)), 1)
    destinationtable = RANGE_PARTITION_TABLE_PREFIX + str(partitionindex)
    RatingsDAO.insert([(userid, itemid, rating)], openconnection, destinationtable)
    if Globals.DEBUG: Globals.printinfo(
        'Inserted rating (UserID: {0}, MovieID: {1}, Rating: {2}), to "{3}" table'.format(userid, itemid, rating,
                                                                                          destinationtable))


def deletepartitions(ratingstablename, openconnection):
    """
    Deletes the partitions and the meta data table. Does NOT drop the Ratings table as per requirement
    :param ratingstablename: name of the ratings table
    :param openconnection: open connection with DB
    :return:None
    """
    with openconnection.cursor() as cur:
        # Delete partitions
        # TODO: Use RatingsDOA to delete the partitions
        temp = MetaDataDAO.select(openconnection, Globals.RANGE_PARTITIONS_KEY)
        rangepartitions = 0 if temp is None else int(temp)
        temp = MetaDataDAO.select(openconnection, Globals.RROBIN_PARTITIONS_KEY)
        robinpartitions = 0 if temp is None else int(temp)

        queries = []
        for i in range(0, robinpartitions):
            queries.append('DROP TABLE IF EXISTS {0}{1}'.format(RROBIN_PARTITION_TABLE_PREFIX, i))
        for i in range(1, rangepartitions + 1):
            queries.append('DROP TABLE IF EXISTS {0}{1}'.format(RANGE_PARTITION_TABLE_PREFIX, i))
        cur.execute('; '.join(queries))
        # Delete MetaData table
        MetaDataDAO.drop(openconnection)
        if Globals.DEBUG: Globals.printinfo('Deleted partitions and Meta Data table')


# Assignment 3

def createrangepartitionandinsertgeneric(conn, col, lower_bound, partition_index, upper_bound, ratingstablename,
                                         dropifexists=True, table_prefix=RANGE_PARTITION_TABLE_PREFIX):
    """
    Creates a new partition table and calls INSERT method of DAO to insert the data
    :param conn: open connection to DB
    :param lower_bound: lower bound on the rating
    :param partition_index: table number. As single single table is split into parts
    :param upper_bound: inclusive upper bound on the rating to insert in the new table
    :param dropifexists: drops the table if exists
    :return:None
    """
    partition_tablename = '{0}{1}'.format(table_prefix, partition_index)
    RatingsDAO.createfromschema(conn, ratingstablename, partition_tablename, dropifexists)
    RatingsDAO.insertwithselectgeneric(col, RatingsDAO.get_column_names(conn, ratingstablename), lower_bound,
                                       upper_bound, partition_tablename, conn, ratingstablename)
    if Globals.DEBUG: Globals.printinfo(
        'Partition {2}: saved values => ({0}, {1}]'.format(lower_bound, upper_bound, partition_index))


def rangepartitiongeneric(tablename, columnname, numberofpartitions, openconnection,
                          tableprefix=RANGE_PARTITION_TABLE_PREFIX, min_value=None, max_value=None):
    """
    Partitions the ratings table in to the given number of partition using Range based partitioning scheme
    Partitioned table names will be starting from 1. If the number of partitions are N, the range of Rating values,
    0 to MAX_RATING, will be split uniformly into N pieces.
    Eg: N = 5
    Partition 1: all movies with a rating => [0, 1]
    Partition 2: all movies with a rating => (1, 2]
    Partition 3: all movies with a rating => (2, 3]
    Partition 4: all movies with a rating => (3, 4]
    Partition 5: all movies with a rating => (4, 5]
    As shown, movies with zero rating will be placed in the first partition
    :param numberofpartitions: Number of partitions
    :param openconnection: open connection to DB
    :return:None
    """
    if numberofpartitions <= 0 or not isinstance(numberofpartitions, int): raise AttributeError(
        "Number of partitions should be a positive integer")

    if min_value is None or max_value is None:
        min_max = RatingsDAO.get_min_max(openconnection, columnname, tablename)
        min_value = min_max[0]
        max_value = min_max[1]

    inc = (max_value - min_value) / numberofpartitions  # This devision doesnt leave any reminder is the assumption
    lower_bound = min_value
    upper_bound = lower_bound + inc

    partition_index = 1
    for i in range(0, numberofpartitions):
        createrangepartitionandinsertgeneric(openconnection, columnname, lower_bound, partition_index, upper_bound,
                                             tablename, True, tableprefix)
        lower_bound += inc
        upper_bound += inc
        partition_index += 1

    # save the rows with min value of sort column in the first partition
    createrangepartitionandinsert(openconnection, min_value - 1, 1, min_value, tablename, False)

    # save the number of partitions in the meta data table
    MetaDataDAO.create(openconnection)  # Create if the table doesnt exist
    MetaDataDAO.upsert(openconnection, Globals.RANGE_PARTITIONS_KEY, numberofpartitions)

    return [min_value, max_value]


def parallel_sort(table, sorting_column_name, output_table, openconnection):
    number_of_partitions = 5  # also dictates the number of threads
    Globals.printinfo(
        'Creating Range partitions on table, {0} into {1} partitions'.format(table, number_of_partitions))
    rangepartitiongeneric(table, sorting_column_name, number_of_partitions, openconnection)

    # output table to save the sorted tuples
    RatingsDAO.createfromschema(openconnection, table, output_table)
    RatingsDAO.addcolumn(openconnection, output_table, 'tupleorder', 'NUMERIC')

    tuple_order_indices = [1]  # starting tuple order index for each partition
    for i in range(1, number_of_partitions):
        tuple_order_indices.append(
            tuple_order_indices[i - 1] + RatingsDAO.numberofratings(openconnection,
                                                                    RANGE_PARTITION_TABLE_PREFIX + str(
                                                                        i)))

    # Create 'number_of_partitions' threads and sort in parallel
    pool = ThreadPool(processes=number_of_partitions)
    for i in range(1, number_of_partitions + 1):
        pool.apply_async(RatingsDAO.sort_rows_and_save,
                         (openconnection, sorting_column_name, 'ASC', tuple_order_indices[i - 1],
                          RANGE_PARTITION_TABLE_PREFIX + str(i), output_table))

    Globals.printinfo('Launched asynchronous threads for sorting. I am done!')


def parallel_join(table1, table2, joincol1, joincol2, output_table, openconnection):
    number_of_partitions = 5  # also dictates the number of threads
    min_max_table1 = RatingsDAO.get_min_max(openconnection, joincol1, table1)
    min_max_table2 = RatingsDAO.get_min_max(openconnection, joincol2, table2)
    min_value = min(min_max_table1[0], min_max_table2[0])  # Pick the min of the minimums
    max_value = max(min_max_table1[1], min_max_table2[1])  # Pick the max of the maximums

    Globals.printinfo(
        'Creating Range partitions on table, {0} into {1} partitions'.format(table1, number_of_partitions))
    rangepartitiongeneric(table1, joincol1, number_of_partitions, openconnection, 'range_tbl1_part', min_value,
                          max_value)

    Globals.printinfo(
        'Creating Range partitions on table, {0} into {1} partitions'.format(table2, number_of_partitions))
    rangepartitiongeneric(table2, joincol2, number_of_partitions, openconnection, 'range_tbl2_part', min_value,
                          max_value)

    # Drop output table if exists
    # RatingsDAO.drop_table(openconnection, output_table)
    RatingsDAO.create_join_table(openconnection, table1, joincol1, table2, joincol2, output_table)

    # Create 'number_of_partitions' threads and sort in parallel
    for i in range(1, number_of_partitions + 1):
        thread.start_new_thread(RatingsDAO.join_tables,
                                (openconnection, table1, joincol1, table2, joincol2, output_table))

    Globals.printinfo('Launched asynchronous threads for join. I am done!')


# Assignment 3 ends

# helpers

def createrangepartitionandinsert(conn, lower_bound, partition_index, upper_bound, ratingstablename, dropifexists=True):
    """
    Creates a new partition table and calls INSERT method of DAO to insert the data
    :param conn: open connection to DB
    :param lower_bound: lower bound on the rating
    :param partition_index: table number. As single single table is split into parts
    :param upper_bound: inclusive upper bound on the rating to insert in the new table
    :param dropifexists: drops the table if exists
    :return:None
    """
    partition_tablename = '{0}{1}'.format(RANGE_PARTITION_TABLE_PREFIX, partition_index)
    RatingsDAO.create(conn, partition_tablename, dropifexists)
    RatingsDAO.insertwithselect(lower_bound, upper_bound, partition_tablename, conn, ratingstablename)
    if Globals.DEBUG: Globals.printinfo(
        'Partition {2}: saved values => ({0}, {1}]'.format(lower_bound, upper_bound, partition_index))


def createrobinpartitionandinsert(conn, sno, ids, ratingstablename, dropifexists=True):
    """
    Creates a new partition table and calls INSERT method of DAO to insert the data
    :param conn: open connection to DB
    :param sno: table number. As single single table is split into parts
    :param ids: IDs of the ratings to insert
    :param dropifexists: drops the table if exists
    :return:None
    """
    partition_tablename = '{0}{1}'.format(RROBIN_PARTITION_TABLE_PREFIX, sno)
    RatingsDAO.create(conn, partition_tablename, dropifexists)
    RatingsDAO.insertids(conn, ids, partition_tablename, ratingstablename)
    if Globals.DEBUG: Globals.printinfo(
        'Partition {0}: saved {1} ratings => {2}...'.format(sno, len(ids), ids[0:6]))


def validaterating(rating):
    # validate rating
    # 1) Should be a positive value and less than or equal to 5.
    # 2) Should have increments of 0.5
    validratings = list(Globals.drange(0, 5.1, 0.5))
    if rating not in validratings:
        print(
            'Rating should be a positive value, less than or equal to 5. It should be one of {0}\n'.format(
                validratings))
        return False
    return True


def fetchrating():
    userid = int(raw_input('Enter rating, user id: '))
    movieid = int(raw_input('movie id: '))
    while True:
        rating = float(raw_input('rating: '))
        if validaterating(rating):
            break
        else:
            print('Try again: ')
    return {'userid': userid, 'movieid': movieid, 'rating': rating}


# menu helpers

def loadratingshelper(conn):
    path = raw_input('Enter data file location: ')
    loadratings(RatingsDAO.TABLENAME, os.path.abspath(path), conn)


def rangepartitionhelper(conn):
    cpartitions = int(raw_input('How many partitions? '))
    rangepartition(RatingsDAO.TABLENAME, cpartitions, conn)


def roundrobinpartitionhelper(conn):
    cpartitions = int(raw_input('How many partitions? '))
    roundrobinpartition(RatingsDAO.TABLENAME, cpartitions, conn)


def rrobininserthelper(conn):
    newrating = fetchrating()
    roundrobininsert(RatingsDAO.TABLENAME, newrating['userid'], newrating['movieid'], newrating['rating'], conn)


def rangeinserthelper(conn):
    newrating = fetchrating()
    rangeinsert(RatingsDAO.TABLENAME, newrating['userid'], newrating['movieid'], newrating['rating'], conn)


def handleexit(*_):
    # TODO: Add a prompt to confirm the selection
    import sys

    sys.exit(0)


def deleteeverythingandexit(conn):
    # TODO: Add a prompt to confirm the selection
    with conn.cursor() as cur:
        cur.execute('drop schema public cascade; create schema public;')
    Globals.printinfo('Dropped all tables')
    handleexit()


def deletepartitionshelper(conn):
    # TODO: Add a prompt to confirm the selection
    deletepartitions(RatingsDAO.TABLENAME, conn)


# Assignment 3 helpers
def parallel_sort_helper(conn):
    parallel_sort('ratings', 'rating', 'ABC', conn)


def parallel_join_helper(conn):
    parallel_join('ratings', 'ratings', 'movieid', 'movieid', 'joined_ratings', conn)


# Middleware
def before_db_creation_middleware():
    # Use it if you want to
    pass


def after_db_creation_middleware(databasename):
    # Use it if you want to
    pass


def before_test_script_starts_middleware(openconnection, databasename):
    openconnection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    MetaDataDAO.create(openconnection)


def after_test_script_ends_middleware(openconnection, databasename):
    # Use it if you want to
    pass


# if __name__ == '__main__':
# try:
#
# # Use this function to do any set up before creating the DB, if any
# before_db_creation_middleware()
#
# create_db(DATABASE_NAME)
#
# # Use this function to do any set up after creating the DB, if any
# after_db_creation_middleware(DATABASE_NAME)
#
# with getopenconnection() as con:
# # Use this function to do any set up before I starting calling your functions to test, if you want to
# before_test_script_starts_middleware(con, DATABASE_NAME)
#
# # Here is where I will start calling your functions to test them. For example,
# loadratings('ratings.dat', con)
# # ###################################################################################
# # Anything in this area will not be executed as I will call your functions directly
# # so please add whatever code you want to add in main, in the middleware functions provided "only"
# # ###################################################################################
#
# # Use this function to do any set up after I finish testing, if you want to
# after_test_script_ends_middleware(con, DATABASE_NAME)
#
# except Exception as detail:
# print "OOPS! This is the error ==> ", detail


if __name__ == '__main__':
    try:
        create_db(DATABASE_NAME)

        options = {
            1: loadratingshelper,
            2: rangepartitionhelper,
            3: roundrobinpartitionhelper,
            4: rangeinserthelper,
            5: rrobininserthelper,
            6: handleexit,
            7: deleteeverythingandexit,
            8: deletepartitionshelper,
            9: parallel_sort_helper,
            10: parallel_join_helper
        }

        with getopenconnection(dbname=DATABASE_NAME) as dbconnection:
            dbconnection.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            MetaDataDAO.create(dbconnection)

            while True:
                choice = raw_input(
                    "\nEnter your choice (number):\n  1) Load Ratings\n  2) Range Partition\n  3) Round Robin Partition\n  4) Range Insert\n  5) Round Robin Insert\n  6) Exit\n  7) Delete everything and Exit\n  8) Delete partitions\n  9) Parallel Sort\n  10) Parallel Join\t: ")

                options[int(choice)](dbconnection)

    except ValueError as detail:
        raise detail
        # Globals.printerror(detail)