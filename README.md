# LocalSaver Backend (FastAPI + MongoDB)

## Overview

LocalSaver is a location-based deals platform where users can discover nearby stores and redeem exclusive deals.
The backend is built with **FastAPI** and **MongoDB** and supports **Admin, Merchant, and User roles**.

The system enables merchants to publish deals, users to redeem them, and administrators to manage the platform.

---

# Tech Stack

Backend Framework
FastAPI

Database
MongoDB

Authentication
JWT (JSON Web Tokens)

Password Security
bcrypt via passlib

API Documentation
Swagger (FastAPI automatic docs)

---

# Project Structure

```
backend/
│
├── routers/
│   ├── admin.py
│   ├── merchants.py
│   ├── users.py
│   ├── stores.py
│   ├── deals.py
│   ├── redemptions.py
│   ├── cities.py
│   ├── categories.py
│   └── search.py
│
├── auth.py
├── database.py
├── seed.py
├── server.py
└── README.md
```

---

# System Roles

The platform supports three roles.

## 1. Admin

Super admin manages the platform.

Capabilities

* Admin login
* View merchants
* Disable merchants
* View users
* Disable users
* View redemptions
* Create stores

---

## 2. Merchant

Business owner who provides deals.

Capabilities

* Merchant registration
* Merchant login
* Create deals
* View merchant stores
* View merchant deals
* View merchant redemptions
* Merchant dashboard analytics

---

## 3. User

Customer using the mobile app.

Capabilities

* User registration
* User login
* Browse stores
* Browse deals
* Redeem deals

---

# Database Collections

MongoDB Database: `localsaver`

Collections used:

```
admins
users
merchants
stores
deals
redemptions
cities
categories
```

---

# Authentication System

Authentication is implemented using **JWT tokens**.

Admin token example:

```
{
 "admin_id": "...",
 "role": "admin",
 "exp": "..."
}
```

Merchant token example:

```
{
 "merchant_id": "...",
 "exp": "..."
}
```

User token example:

```
{
 "user_id": "...",
 "exp": "..."
}
```

---

# Core APIs

## Public APIs

```
GET /stores
GET /deals
GET /store/{store_id}/deals
```

---

## User APIs

```
POST /users/register
POST /users/login
POST /redeem-deal
```

---

## Merchant APIs

```
POST /merchants/register
POST /merchants/login

POST /merchant/deal

GET /merchant/deals
GET /merchant/stores
GET /merchant/redemptions

GET /merchant/dashboard
```

---

## Admin APIs

```
POST /admin/login

GET /admin/merchants
POST /admin/merchant/disable

GET /admin/users
POST /admin/user/disable

GET /admin/redemptions

POST /admin/store
```

---

# Running the Project

Start MongoDB

```
mongod
```

Start the backend

```
python -m uvicorn server:app --reload
```

Open API docs

```
http://127.0.0.1:8000/docs
```

---

# Key Features Implemented

Location-based store discovery
Merchant deal management
Deal redemption tracking
Admin platform controls
JWT authentication
Role-based access control
Merchant dashboard analytics

---

# Future Improvements

User geolocation filtering
Deal expiry automation
Push notifications
Analytics dashboard
Payment integration
Mobile app integration

---

# Author

LocalSaver Backend
Built using FastAPI and MongoDB
