import datetime
from flask import Flask, render_template, request, redirect, jsonify
import heapq
import json
import math

app = Flask(__name__)

assignments = []
pq = []
notes = {}  # key: assignment name, value: note string

CAREER_INTERESTS = []  # set by student profile

RESEARCH_SOURCES = [
    {
        "title": "Dunlosky et al. (2013): Effective Learning Techniques",
        "url": "https://doi.org/10.1177/1529100612453266",
    },
    {
        "title": "Roediger & Karpicke (2006): Testing Effect",
        "url": "https://doi.org/10.1111/j.1467-9280.2006.01693.x",
    },
    {
        "title": "Cepeda et al. (2006): Distributed Practice Review",
        "url": "https://doi.org/10.1037/0033-2909.132.3.354",
    },
    {
        "title": "Aeon et al. (2021): Time Management Meta-analysis",
        "url": "https://doi.org/10.1371/journal.pone.0245066",
    },
    {
        "title": "CDC (Mar 10, 2026): Physical Activity Benefits for Children",
        "url": "https://www.cdc.gov/physical-activity-basics/health-benefits/children.html",
    },
    {
        "title": "AASM Consensus (2016): Recommended Sleep for Teens",
        "url": "https://doi.org/10.5664/jcsm.5866",
    },
]


def _parse_date(date_str):
    try:
        return datetime.datetime.strptime(str(date_str), '%Y-%m-%d').date()
    except Exception:
        return None


def _build_spaced_dates(due_date, today):
    if due_date is None:
        return []

    spaced_dates = []
    for days_before_due in (7, 3, 1):
        candidate = due_date - datetime.timedelta(days=days_before_due)
        if candidate >= today:
            spaced_dates.append(candidate.isoformat())

    if not spaced_dates and due_date >= today:
        spaced_dates.append(today.isoformat())

    return spaced_dates


def _compute_study_coach(assignments_list, scores_by_assignment):
    today = datetime.date.today()
    pending = []
    total_pending_hours = 0.0

    for assignment in assignments_list:
        if assignment.get('status') == 'done':
            continue

        due_date = _parse_date(assignment.get('due_date'))
        if due_date:
            days_left = (due_date - today).days
        else:
            days_left = 999

        hours = float(assignment.get('hours', 1) or 1)
        total_pending_hours += hours
        focus_blocks = max(1, math.ceil((hours * 60) / 40))

        score = scores_by_assignment.get(assignment['assignment'], 0)
        urgency = "High" if days_left <= 2 else ("Medium" if days_left <= 5 else "Low")

        if days_left <= 1:
            next_action = "Run a closed-book retrieval check, then submit a final pass."
        elif days_left <= 3:
            next_action = "Do retrieval practice now and schedule one spaced review tomorrow."
        else:
            next_action = "Start early with one focus block and spaced reviews this week."

        pending.append({
            "assignment": assignment['assignment'],
            "course": assignment['course'],
            "due_date": assignment.get('due_date'),
            "days_left": days_left,
            "focus_blocks": focus_blocks,
            "retrieval_rounds": 2 if days_left > 1 else 1,
            "spaced_dates": _build_spaced_dates(due_date, today),
            "interleave_hint": "Pair this with a different course in your next block.",
            "priority_score": round(score, 2),
            "urgency": urgency,
            "next_action": next_action,
        })

    pending_sorted = sorted(pending, key=lambda item: (item["days_left"], -item["priority_score"]))
    top_actions = pending_sorted[:3]

    if pending_sorted:
        non_negative_days = [item["days_left"] for item in pending_sorted if item["days_left"] >= 0]
        furthest_due = max(non_negative_days) if non_negative_days else 1
        planning_days = max(1, min(7, furthest_due if furthest_due > 0 else 1))
        daily_minutes = math.ceil((total_pending_hours * 60) / planning_days)
    else:
        daily_minutes = 0

    weekly_protocol = [
        {"id": "retrieval", "label": "Use retrieval practice (self-test, no notes first)"},
        {"id": "spacing", "label": "Schedule at least 2 spaced reviews per major task"},
        {"id": "interleave", "label": "Alternate subjects instead of one long single-subject block"},
        {"id": "timeblock", "label": "Protect daily study time blocks in your calendar"},
        {"id": "sleep", "label": "Sleep target: 8+ hours/night (teens: 8-10)"},
        {"id": "movement", "label": "Add movement breaks to improve attention and memory"},
    ]

    return {
        "top_actions": top_actions,
        "assignment_plans": pending_sorted,
        "daily_minutes_target": daily_minutes,
        "weekly_protocol": weekly_protocol,
    }

@app.template_filter('days_from_today')
def days_from_today(date_str):
    try:
        due = datetime.datetime.strptime(str(date_str), '%Y-%m-%d')
        return (due - datetime.datetime.now()).days
    except:
        return 999

@app.route('/')
def home():
    score_map = {}
    if pq:
        max_score = max(score for score, _ in pq)
        normalized_pq = []
        for score, assignment_name in pq:
            score_map[assignment_name] = max(score_map.get(assignment_name, 0), score)
            normalized_score = (score / max_score) * 100
            normalized_pq.append((normalized_score, assignment_name))
        sorted_pq = sorted(normalized_pq, reverse=True)
    else:
        sorted_pq = []

    study_coach = _compute_study_coach(assignments, score_map)

    # Build calendar events from assignments
    calendar_events = []
    for a in assignments:
        calendar_events.append({
            'title': f"{a['course']}: {a['assignment']}",
            'date': a['due_date'],
            'course': a['course'],
        })

    return render_template(
        'index.html',
        assignments=assignments,
        pq=sorted_pq,
        notes=notes,
        calendar_events=json.dumps(calendar_events),
        career_interests=CAREER_INTERESTS,
        study_coach=study_coach,
        research_sources=RESEARCH_SOURCES,
    )


@app.route('/add', methods=['POST'])
def add_assignment():
    career_relevance_raw = request.form.get('career_relevance', '').strip().lower()
    career_relevant = 1 if career_relevance_raw in ['yes', '1', 'true'] else 0

    new_assignment = {
        'course': request.form['course'],
        'assignment': request.form['assignment'],
        'due_date': request.form['due_date'],
        'hours': float(request.form['hours']),
        'points': int(request.form['points']),
        'grade_weight': float(request.form['grade_weight']),
        'course_importance': int(request.form['course_importance']),
        'counts_for_major': int(request.form.get('counts_for_major', 1)),  # 1 = yes, 0 = no
        'credits': float(request.form.get('credits', 3)),
        'career_relevant': career_relevant,
        'status': 'not started',  # not started / in progress / done
    }

    due_date = datetime.datetime.strptime(new_assignment['due_date'], '%Y-%m-%d')
    today = datetime.datetime.now()
    days_remaining = (due_date - today).days

    if days_remaining <= 0:
        time_pressure = 1000
    elif days_remaining <= 1:
        time_pressure = 10
    elif days_remaining <= 3:
        time_pressure = 5
    else:
        time_pressure = 1 / days_remaining

    efficiency = new_assignment['points'] / max(new_assignment['hours'], 0.1)
    grade_impact = (new_assignment['grade_weight'] / 100) * new_assignment['course_importance']
    major_boost = 1.5 if new_assignment['counts_for_major'] else 1.0
    credit_weight = new_assignment['credits'] / 3.0  # normalized around 3 credits
    career_boost = 1.3 if new_assignment['career_relevant'] else 1.0

    score = efficiency * time_pressure * grade_impact * major_boost * credit_weight * career_boost

    heapq.heappush(pq, (score, new_assignment['assignment']))
    assignments.append(new_assignment)
    return redirect('/')


@app.route('/update_status', methods=['POST'])
def update_status():
    data = request.get_json()
    assignment_name = data.get('assignment')
    new_status = data.get('status')
    for a in assignments:
        if a['assignment'] == assignment_name:
            a['status'] = new_status
            break
    return jsonify({'ok': True})


@app.route('/save_note', methods=['POST'])
def save_note():
    data = request.get_json()
    assignment_name = data.get('assignment')
    note_text = data.get('note', '')
    notes[assignment_name] = note_text
    return jsonify({'ok': True})


@app.route('/delete_assignment', methods=['POST'])
def delete_assignment():
    data = request.get_json()
    assignment_name = data.get('assignment')
    global assignments, pq
    assignments = [a for a in assignments if a['assignment'] != assignment_name]
    pq = [(s, n) for s, n in pq if n != assignment_name]
    heapq.heapify(pq)
    if assignment_name in notes:
        del notes[assignment_name]
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(debug=True)
