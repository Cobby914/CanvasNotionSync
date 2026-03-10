# Canvas → Notion Sync

Automatically sync your Canvas assignments into Notion so your tasks stay organized in one place.

This project pulls assignments from the Canvas LMS API and inserts or updates them in a Notion database. It can also create tasks in a task tracker with calculated priority and effort values.

The sync runs automatically using GitHub Actions.

# Features

*  Fetch assignments from Canvas
*  Sync assignments to a Notion database
*  Automatically create tasks in a task tracker
*  Daily automatic sync via GitHub Actions
*  Prevent duplicate assignments by checking existing entries
*  Optional priority and effort calculation based on due dates and points

# Upcoming Assignment Filtering
Only assignments with future due dates are processed. This prevents past assignments from cluttering the task tracker and ensures that Notion focuses on upcoming work that still requires attention.

# Assignments Database Sync

The Assignments Database acts as a raw data mirror of Canvas assignments. For each assignment the following information is stored:
* Assignment title (prefixed with course name)
* Canvas assignment ID
* Course ID
* Points possible
* Canvas URL
* Due date
* Assignment description (converted from HTML to Notion blocks)

Assignments are deduplicated using the Canvas Assignment ID, ensuring existing entries are not recreated.

# Task Tracker Database Sync

The Task Tracker Database converts Canvas assignments into productivity tasks.

Each task includes the following fields:

* Task name
* Due date
* Effort level
* Priority
* Status
* Task type

Tasks are deduplicated using the generated task title [Course] Assignment Name.

Existing tasks are updated only when key attributes change (due date, priority, or effort), reducing unnecessary API operations.

# Automatic Priority Calculation

Task priority is determined dynamically based on how close the due date is:

Days Until Due	Priority
```
≤ 3 days	High
≤ 7 days	Medium
> 7 days	Low
```
This ensures assignments approaching their deadlines are automatically surfaced as higher priority tasks.

# Effort Estimation

Effort level is estimated using the assignment point value:

Points	Effort
```
≥ 80	Large
≥ 30	Medium
< 30	Small
```
This provides a quick estimate of workload when planning tasks.

# Task Type Detection

Assignments are automatically categorized based on keywords in their title:
* Exam – assignments containing “exam”, “midterm”, or “final”
* Homework – all other assignments

# Automated Scheduling via GitHub Actions

The synchronization script is designed to run via a GitHub Actions workflow. Environment variables store API tokens and database identifiers, allowing secure automated execution without exposing credentials.
This ensures that Canvas assignments remain synchronized with Notion on a recurring schedule.
