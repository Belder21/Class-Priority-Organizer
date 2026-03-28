from flask import Flask, render_template, request, redirect

# LinK: http://127.0.0.1:5000

app = Flask(__name__)

assignments = []

@app.route('/')
def home():
    return render_template('index.html', assignments=assignments)


@app.route('/add', methods=['POST'])
def add_assignment():
    new_assignment = {
        'course': request.form['course'],
        'assignment': request.form['assignment'],
        'due_date': request.form['due_date'],
        'hours': request.form['hours'],
        'points': request.form['points']
    }

    assignments.append(new_assignment)
    return redirect('/')

if __name__ == '__main__':
    app.run(debug=True)