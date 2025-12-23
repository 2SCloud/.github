import requests
from collections import defaultdict

ORG = "2SCloud"
README_FILE = "../README.md"

repos = requests.get(f"https://api.github.com/orgs/{ORG}/repos?per_page=100").json()

langs = defaultdict(int)
for repo in repos:
    repo_name = repo['name']
    lang_data = requests.get(f"https://api.github.com/repos/{ORG}/{repo_name}/languages").json()
    for lang, size in lang_data.items():
        langs[lang] += size

top_langs = sorted(langs.items(), key=lambda x: x[1], reverse=True)

with open(README_FILE, "w") as f:
    f.write(f"# 🚀 {ORG}\n\n")
    f.write("Welcome to the GitHub organization!\n\n")
    
    f.write("## 🧠 Top Languages\n\n")
    f.write("| Language | Bytes |\n|----------|-------|\n")
    for lang, size in top_langs[:8]:
        f.write(f"| {lang} | {size} |\n")
    
    f.write("\n## 📦 Popular Repositories\n\n")
    # On affiche les 5 repos avec le plus de stars
    sorted_repos = sorted(repos, key=lambda x: x['stargazers_count'], reverse=True)
    for repo in sorted_repos[:5]:
        f.write(f"- [{repo['name']}]({repo['html_url']}) - ⭐ {repo['stargazers_count']}\n")
    
    f.write("\n## 🤝 Contributing\n")
    f.write("Contributions are welcome! Please see `CONTRIBUTING.md`.\n")
