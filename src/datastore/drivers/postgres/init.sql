CREATE EXTENSION "hstore";
CREATE EXTENSION "ltree";
CREATE EXTENSION "uuid-ossp";
CREATE DATABASE freenas;
CREATE USER freenas;
GRANT ALL ON DATABASE freenas TO freenas;