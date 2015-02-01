__author__ = 'Nitin Pasumarthy'

import psycopg2
from itertools import islice

import RatingsDAO
import Globals

CHUNK_SIZE = 140  # bytes
MAX_LINES_COUNT_READ = 4  # Maximum number of lines to read into memory
DATABASE_NAME = 'dds_assgn1'
MAX_RATING = 5.0

DEBUG = True


def readfilebyline(filepath):
    """
    A simple file read function
    :param filepath: relative or abs path of the fle to read
    :return:None
    """
    with open(filepath) as ratings_file:
        for line in ratings_file:
            print(line)


def manualchunkread(filepath):
    """
    Reads a file in chunks as defined by CHUNK_SIZE variable
    :param filepath: relative or abs path of file to read
    :return:None
    """
    f = open(filepath, 'r')
    while True:
        data = f.read(CHUNK_SIZE)
        if not data:
            break
        lines = data.split('\n')
        traceback_amount = len(lines[-1])
        f.seek(-traceback_amount, 1)
        for line in lines[0:-1]:
            print(line)
            print()
        print
    f.close()


def getnextchunk(filepath):
    """
    Reads files in chunks in an efficient manner using isslice method.
    Uses yield to return the next chunk if an existing file is being read
    :param filepath: relative or abs path of the file to read
    :return:Chunk of lines using yield
    """
    with open(filepath) as f:
        while True:
            lines = list(islice(f, MAX_LINES_COUNT_READ))
            if len(lines) < MAX_LINES_COUNT_READ:
                break
            yield lines


def getconnection(dbname='postgres'):
    """
    Connects to dds_assgn1 database using postgres user
    :return: Open DB connection
    """
    return psycopg2.connect("dbname='" + dbname + "' user='postgres' host='localhost' password='1234'")


def createdb(dbname):
    """
    We create a DB by connecting to the default user and database of Postgres
    The function first checks if an existing database exists for a given name, else creates it.
    :return:None
    """
    # Connect to the default database
    con = getconnection()
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


def loadratings(filepath, conn):
    """
    Loads the file into DB
    :param filepath: relative or abs path of the file to load
    :param conn: open connection to DB
    :return: None
    """
    RatingsDAO.create(conn)
    for lines in getnextchunk(filepath):
        ratings = []
        for line in lines:
            rating = line.split('::')[0:3]
            ratings.append(rating)
        RatingsDAO.insert(ratings, conn)


def createtableandinsert(conn, lower_bound, sno, upper_bound, dropifexists=True):
    """
    Creates a new partition table and calls INSERT method of DAO to insert the data
    :param conn: open connection to DB
    :param lower_bound: lower bound on the rating
    :param sno: table number. As single single table is split into parts
    :param upper_bound: inclusive upper bound on the rating to insert in the new table
    :param dropifexists: drops the table if exists
    :return:None
    """
    partition_tablename = 'range_part{0}'.format(sno)
    RatingsDAO.create(conn, partition_tablename, dropifexists)
    RatingsDAO.insertwithselect(lower_bound, upper_bound, partition_tablename, conn)
    if DEBUG: Globals.printinfo('Partition {2}: saved ratings => ({0}, {1}]'.format(lower_bound, upper_bound, sno))


def rangepartition(n, conn):
    if n <= 0 or not isinstance(n, int): raise AttributeError("Number of partitions should be a positive integer")

    inc = round(float(MAX_RATING) / n, 1)  # precision restricted to 1 decimal as Ratings have 0.5 increments
    lower_bound = 0.0
    upper_bound = lower_bound + inc

    sno = 1
    while upper_bound <= MAX_RATING:
        createtableandinsert(conn, lower_bound, sno, upper_bound)
        lower_bound += inc
        upper_bound += inc
        sno += 1

    # If number of partitions is not divisible by MAX_RATING, the last partition will be missed due to rounding
    if lower_bound != MAX_RATING:
        createtableandinsert(conn, lower_bound, sno, MAX_RATING)

    # save the movies with zero rating in the first partition
    createtableandinsert(conn, -1, 1, 0, False)


if __name__ == '__main__':
    try:
        # conn = psycopg2.connect("dbname='mydb' user='postgres' host='localhost' password='1234'")
        # cur = conn.cursor()
        # cur.execute("""SELECT * from weather""")
        # rows = cur.fetchall()
        # print "\nShow me the databases:\n"
        # for row in rows:
        # print "   ", row
        # cur.close()
        # conn.close()

        createdb(DATABASE_NAME)

        with getconnection(DATABASE_NAME) as conn:
            loadratings('test_data.dat', conn)
            rangepartition(1, conn)
    except Exception as detail:
        Globals.printerror(detail)