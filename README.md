Coven AI — PHC Healthcare Resource Management

Coven AI is a Flask-based healthcare resource management platform designed around a network of Primary Health Centres (PHCs).

The application provides two main experiences:

Citizen portal — discover PHCs, search facilities, view medicine catalog information, find nearby PHCs, and ask Coven AI for help locating available beds or medicines.

PHC admin portal — manage beds and medicine stock for an assigned PHC, monitor low/out-of-stock medicines, record usage, view analytics, and use Coven AI as an inventory assistant.

The application uses MySQL for healthcare data, Google Gemini for AI-assisted responses, Leaflet for maps, and Matplotlib for analytics charts.

Features

Citizen Portal

Citizen account signup and login

Browse all registered PHCs

Search PHCs by keyword

View individual PHC details

View the medicine catalog

Find PHCs near the user's current location

Interactive Leaflet maps

Distance calculation between the user and PHCs

Ask Coven AI healthcare-resource questions

Request specific quantities of:

General beds

ICU beds

Oxygen beds

Medicines

Multi-PHC allocation when one PHC cannot fulfil the complete request

Location-aware allocation when browser location is available

PHC Admin Portal

PHC-specific admin authentication

View assigned PHC information

View bed availability

View medicine stock and expiry dates

Automatic low-stock and out-of-stock warnings

Update available beds

Update medicine quantities

Record bed usage

Record medicine usage

Coven AI inventory assistant

PHC-restricted AI responses

Analytics dashboard

Stock status charts

Medicine usage/stock visualizations

Bed availability charts

Predictive stock analysis based on recent usage

AI and Prediction

Coven AI is integrated using Google's Gemini API.

The application uses different data scopes for admins and citizens:

Admin AI receives data belonging to the logged-in PHC and is instructed not to expose information from other PHCs.

Citizen AI can answer general PHC/network questions using citizen-safe information.

Specific medicine and bed requests are handled through deterministic database allocation logic before Gemini is used to phrase the final response.

Predictive stock analysis uses medicine usage recorded over the previous 30 days to estimate average daily usage and approximate days of stock remaining.

Prediction statuses include:

OUT_OF_STOCK

CRITICAL

WARNING

MONITOR

STABLE

Tech Stack

Technology

Purpose

Python

Backend application

Flask

Web framework

MySQL

Database and healthcare resource storage

mysql-connector-python

MySQL connectivity

Google Gemini / google-genai

AI assistant and response generation

Werkzeug

Password hashing

Matplotlib

Analytics charts

Leaflet.js

Interactive maps

HTML/CSS/JavaScript

Frontend UI

Project Structure

.
├── main(1).py
├── requirements.txt
└── README.md

The current application renders its HTML/CSS/JavaScript templates directly from the Flask Python application, so a separate templates/ directory is not required by this version.

Database

The application expects a MySQL database named:

PHC_FAKE

The code works with healthcare data including tables/views such as:

phcs
beds
medicine_catalog
phc_medicine_stock
medicine_usage_history
admin_phc_shortage_view
best_medicine_redistribution

The application also creates the medicine_usage_history table automatically if it does not already exist.

Important

A complete SQL schema/seed-data file is not included in the supplied project files, so the required MySQL database and its core tables/views must already exist before running the application.

Installation

1. Clone the repository

git clone <YOUR-GITHUB-REPOSITORY-URL>
cd <YOUR-REPOSITORY-NAME>

2. Create a virtual environment

Windows:

python -m venv venv
venv\Scripts\activate

macOS/Linux:

python3 -m venv venv
source venv/bin/activate

3. Install dependencies

pip install -r requirements.txt

The current requirements include:

Flask
mysql-connector-python
Werkzeug
matplotlib
google-genai

Configuration

Before running the application, configure your database and Gemini credentials.

The application needs:

DB_HOST
DB_USER
DB_PASSWORD
DB_NAME
GEMINI_API_KEY
GEMINI_MODEL
FLASK_SECRET_KEY

For deployment, these values should be supplied as environment variables rather than committed to GitHub.

A recommended configuration pattern is:

import os

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "PHC_FAKE")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

app.secret_key = os.getenv("FLASK_SECRET_KEY")

Security Warning

Do not commit database passwords, Gemini API keys, Flask secret keys, or other credentials to GitHub.

If credentials have previously been committed to a public repository, rotate/revoke them and replace them with environment variables.

Running Locally

Start the Flask application with:

python main(1).py

Then open the local address shown by Flask in your browser, typically:

http://127.0.0.1:5000

Main Routes

Public / Authentication

/
 /signup
 /login
 /logout

Admin

/admin
/admin/beds
/admin/stock
/admin/warnings
/admin/update_beds
/admin/update_stock
/admin/mark_beds
/admin/mark_medicine
/admin/ai
/admin/analytics

Citizen

/customer
/customer/phcs
/customer/phc/<phc_id>
/customer/search
/customer/catalog
/customer/near_me
/customer/ai

API

/api/predictive-stock
/api/coven
/api/coven-citizen
/api/nearby-phcs

How Resource Allocation Works

When a citizen requests a specific resource, the application first identifies the request.

For beds, it detects the requested ward type:

general
icu
oxygen

For medicines, it matches the requested medicine against the medicine catalog.

The application then:

Finds PHCs with the requested resource available.

Calculates distance when the citizen's latitude and longitude are available.

Orders candidate PHCs based on proximity or district preference.

Allocates the requested quantity across PHCs.

Reports where the citizen should collect the resource and how much to collect at each location.

Uses Gemini to turn the confirmed allocation data into a concise natural-language response.

The allocation data remains the factual source; Gemini is not responsible for inventing availability, quantities, PHC names, or distances.

Predictive Stock Analysis

The admin analytics system uses medicine usage history from the previous 30 days.

The basic calculation is:

Average Daily Usage = Units Used in Last 30 Days / 30

Predicted Days Remaining =
Current Stock / Average Daily Usage

The result is categorized into stock-risk statuses so administrators can identify medicines that may require attention.

Maps and Location

The citizen "Near Me" functionality uses browser geolocation.

Coordinates are used to calculate distances to PHCs using the Haversine formula.

Leaflet is used to display PHCs and resource locations on interactive maps.

Location access is optional. If coordinates are unavailable, the application can still provide PHC information without distance-based sorting.

Authentication and Access Control

The application uses Flask sessions to distinguish between:

admin

customer

Admin routes require an authenticated admin session and use the PHC associated with that admin account.

Citizen routes require an authenticated customer session.

Passwords are handled using Werkzeug's password hashing utilities rather than storing plain-text passwords through the authentication functions.

Analytics

The admin analytics dashboard provides visual summaries of:

Medicine stock status

Medicine quantities

Bed availability

PHC-level inventory indicators

Predictive stock information

Charts are generated with Matplotlib.

Deployment

For a production deployment, configure the following as environment variables in your hosting platform:

DB_HOST
DB_USER
DB_PASSWORD
DB_NAME
GEMINI_API_KEY
GEMINI_MODEL
FLASK_SECRET_KEY

Install dependencies with:

pip install -r requirements.txt

A typical Python web-service start command is:

python main(1).py

For hosting platforms that provide a PORT environment variable, the Flask application should be configured to bind to:

host="0.0.0.0"
port=int(os.environ.get("PORT", 5000))

The database must also be reachable from the deployed server.

Privacy and Data Boundaries

The application intentionally separates information available to admins and citizens.

For example, citizen-facing general AI data does not include:

network-wide bed quantities

medicine stock quantities

stock warnings

Admin AI is restricted to the PHC associated with the logged-in admin account.

These application-level restrictions are intended to prevent cross-PHC information exposure.

Known Requirements

Before the application can be fully used, make sure you have:

Python installed

MySQL server/database available

Required MySQL tables and views populated

Gemini API access configured

Browser location permission enabled for "Near Me" functionality

Internet access for Leaflet/Google Fonts/remote visual assets used by the frontend

Future Improvements

Possible extensions include:

Move all secrets to environment variables

Add a dedicated .env configuration workflow for local development

Add a complete database schema and seed-data script

Add automated tests

Use a production WSGI server such as Gunicorn

Add CSRF protection to forms

Add stronger session/security configuration

Add role and PHC-management administration

Add more advanced demand forecasting

Add stock redistribution workflows and audit logs

Add persistent deployment-oriented database configuration

Disclaimer

Coven AI is a healthcare resource-management and information platform. It is not a medical diagnosis or treatment system. The AI assistant is designed to help users locate healthcare resources and understand resource availability, not to replace qualified medical professionals.
