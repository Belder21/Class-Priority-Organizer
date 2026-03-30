import datetime
from flask import Flask, render_template, request, redirect, jsonify
import heapq
import json
import os

app = Flask(__name__)

assignments = []
pq = []
notes = {}  # key: assignment name, value: note string

CAREER_INTERESTS = []  # set by student profile

@app.template_filter('days_from_today')
def days_from_today(date_str):
    try:
        due = datetime.datetime.strptime(str(date_str), '%Y-%m-%d')
        return (due - datetime.datetime.now()).days
    except:
        return 999

@app.route('/')
def home():
    if pq:
        max_score = max(score for score, _ in pq)
        normalized_pq = []
        for score, assignment_name in pq:
            normalized_score = (score / max_score) * 100
            normalized_pq.append((normalized_score, assignment_name))
        sorted_pq = sorted(normalized_pq, reverse=True)
    else:
        sorted_pq = []

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
