-- Migration 0001: Create Research Data Platform schemas
-- Phase 1: Research Data Platform
-- Creates the 5 core schemas for the data platform

CREATE SCHEMA IF NOT EXISTS meta;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
