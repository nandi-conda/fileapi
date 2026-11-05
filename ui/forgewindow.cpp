#include "forgewindow.h"
#include "api.h"

#include <QComboBox>
#include <QDateTime>
#include <QFontDatabase>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QLocale>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSplitter>
#include <QStackedWidget>
#include <QStatusBar>
#include <QStyle>
#include <QTabWidget>
#include <QToolBar>
#include <QTreeWidget>
#include <QVBoxLayout>

namespace {
constexpr int PathRole = Qt::UserRole;
constexpr int DirRole = Qt::UserRole + 1;
constexpr int ShaRole = Qt::UserRole + 2;

QString humanSize(qint64 n)
{
    return QLocale::c().formattedDataSize(n, 1, QLocale::DataSizeTraditionalFormat);
}

QString repoPath(const QString &repo) { return "repos/" + repo; }
}  // namespace

ForgeWindow::ForgeWindow(QWidget *parent) : QMainWindow(parent), m_api(new Api(this))
{
    setWindowTitle("fileapi forge");

    // ---- top bar
    auto *bar = addToolBar("Account");
    bar->setMovable(false);
    auto *brand = new QLabel("<b>fileapi</b> forge");
    brand->setContentsMargins(8, 0, 16, 0);
    bar->addWidget(brand);
    auto *spacer = new QWidget;
    spacer->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    bar->addWidget(spacer);
    m_who = new QLabel;
    m_who->setContentsMargins(0, 0, 8, 0);
    bar->addWidget(m_who);
    m_signIn = new QPushButton("Sign in");
    bar->addWidget(m_signIn);
    connect(m_signIn, &QPushButton::clicked, this, [this] {
        m_api->hasToken() ? m_api->signOut() : m_api->signIn();
    });

    // ---- repo list
    auto *split = new QSplitter;
    auto *left = new QWidget;
    auto *leftLay = new QVBoxLayout(left);
    leftLay->setContentsMargins(8, 8, 0, 8);
    leftLay->addWidget(new QLabel("<b>Repositories</b>"));
    m_repos = new QListWidget;
    leftLay->addWidget(m_repos);
    split->addWidget(left);
    connect(m_repos, &QListWidget::currentItemChanged, this, [this](QListWidgetItem *it) {
        if (it && it->data(PathRole).toString() != m_repo)
            openRepo(it->data(PathRole).toString());
    });

    // ---- repo view
    m_main = new QStackedWidget;
    auto *empty = new QLabel("Pick a repository on the left.");
    empty->setAlignment(Qt::AlignCenter);
    m_main->addWidget(empty);

    auto *repoView = new QWidget;
    auto *lay = new QVBoxLayout(repoView);
    lay->setContentsMargins(8, 8, 8, 8);
    m_title = new QLabel;
    QFont tf = m_title->font();
    tf.setPointSizeF(tf.pointSizeF() * 1.5);
    tf.setBold(true);
    m_title->setFont(tf);
    m_title->setTextInteractionFlags(Qt::TextSelectableByMouse);
    lay->addWidget(m_title);
    m_desc = new QLabel;
    m_desc->setWordWrap(true);
    lay->addWidget(m_desc);

    auto *row = new QHBoxLayout;
    row->addWidget(new QLabel("Branch:"));
    m_ref = new QComboBox;
    m_ref->setMinimumWidth(160);
    m_ref->setEditable(true);  // also accepts a commit sha or tag typed in
    row->addWidget(m_ref);
    row->addSpacing(16);
    row->addWidget(new QLabel("Clone:"));
    m_clone = new QLineEdit;
    m_clone->setReadOnly(true);
    row->addWidget(m_clone, 1);
    lay->addLayout(row);
    connect(m_ref, &QComboBox::textActivated, this, [this] {
        browse(QString());
        loadLog();
    });

    m_tabs = new QTabWidget;
    // Files tab: breadcrumb, then either a directory listing or a file viewer.
    auto *files = new QWidget;
    auto *fl = new QVBoxLayout(files);
    fl->setContentsMargins(4, 4, 4, 4);
    m_crumbs = new QLabel;
    m_crumbs->setTextFormat(Qt::RichText);
    connect(m_crumbs, &QLabel::linkActivated, this, [this](const QString &link) { browse(link.mid(1)); });
    fl->addWidget(m_crumbs);
    m_filesStack = new QStackedWidget;
    m_entries = new QTreeWidget;
    m_entries->setHeaderLabels({"Name", "Size"});
    m_entries->setRootIsDecorated(false);
    m_entries->setUniformRowHeights(true);
    m_entries->header()->setStretchLastSection(false);
    m_entries->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_entries->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    connect(m_entries, &QTreeWidget::itemActivated, this, [this](QTreeWidgetItem *it) {
        browse(it->data(0, PathRole).toString());
    });
    m_filesStack->addWidget(m_entries);
    m_viewer = new QPlainTextEdit;
    m_viewer->setReadOnly(true);
    m_viewer->setLineWrapMode(QPlainTextEdit::NoWrap);
    m_viewer->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
    m_filesStack->addWidget(m_viewer);
    fl->addWidget(m_filesStack, 1);
    m_tabs->addTab(files, "Files");

    m_commits = new QTreeWidget;
    m_commits->setHeaderLabels({"Message", "Author", "Date", "Commit"});
    m_commits->setRootIsDecorated(false);
    m_commits->setUniformRowHeights(true);
    m_commits->header()->setStretchLastSection(false);
    m_commits->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    for (int i = 1; i < 4; ++i)
        m_commits->header()->setSectionResizeMode(i, QHeaderView::ResizeToContents);
    m_commits->setToolTip("Double-click a commit to browse the files as they were then.");
    connect(m_commits, &QTreeWidget::itemActivated, this, [this](QTreeWidgetItem *it) {
        m_ref->setCurrentText(it->data(0, ShaRole).toString().left(12));
        m_tabs->setCurrentIndex(0);
        browse(QString());
        loadLog();
    });
    m_tabs->addTab(m_commits, "Commits");
    connect(m_tabs, &QTabWidget::currentChanged, this, [this](int i) {
        if (i == 1 && !m_repo.isEmpty() && m_ref->count() > 0
            && (m_commits->topLevelItemCount() == 0 || m_tabs->tabText(1).endsWith("(error)")))
            loadLog();
    });
    lay->addWidget(m_tabs, 1);
    m_main->addWidget(repoView);
    split->addWidget(m_main);
    split->setStretchFactor(1, 1);
    split->setSizes({260, 900});
    setCentralWidget(split);

    m_status = new QLabel;
    statusBar()->addWidget(m_status, 1);

    loadMe();
    loadRepos();
}

void ForgeWindow::status(const QString &text, bool error)
{
    m_status->setText(text);
    m_status->setStyleSheet(error ? "color: #c62828;" : "");
}

QString ForgeWindow::ref() const
{
    const QString r = m_ref->currentText().trimmed();
    return r.isEmpty() ? "HEAD" : r;
}

void ForgeWindow::loadMe()
{
    m_api->get("me", {}, this, [this](const QJsonObject &me, const QString &error) {
        const bool in = error.isEmpty() && me.value("signed_in").toBool();
        const QString admin = me.value("admin").toString();
        m_who->setText(in ? "Signed in as " + admin : "Public repos only");
        m_signIn->setText(in ? "Sign out" : "Sign in");
        m_signIn->setToolTip(in ? QString() : "Only " + admin + " can sign in, to see private repos.");
    });
}

void ForgeWindow::loadRepos()
{
    status("Loading repositories…");
    m_api->get("repos", {}, this, [this](const QJsonObject &body, const QString &error) {
        if (!error.isEmpty())
            return status("Could not list repositories: " + error, true);
        m_repos->clear();
        const QJsonArray repos = body.value("repos").toArray();
        for (const auto &v : repos) {
            const QJsonObject r = v.toObject();
            const QString name = r.value("repo").toString();
            const bool pub = r.value("public").toBool();
            auto *it = new QListWidgetItem(name + (pub ? "" : "  (private)"));
            it->setData(PathRole, name);
            it->setToolTip(r.value("description").toString());
            m_repos->addItem(it);
        }
        status(repos.isEmpty() ? "No repositories you can see yet." : QStringLiteral("%1 repositories").arg(repos.size()));
        // Restore #<project>/<repo>[/path] from the page URL.
        const QString hash = m_api->locationHash();
        const QStringList parts = hash.split('/', Qt::SkipEmptyParts);
        if (parts.size() >= 2) {
            const QString repo = parts[0] + "/" + parts[1];
            m_pendingPath = parts.mid(2).join('/');
            for (int i = 0; i < m_repos->count(); ++i)
                if (m_repos->item(i)->data(PathRole).toString() == repo)
                    m_repos->setCurrentRow(i);
        }
    });
}

void ForgeWindow::openRepo(const QString &repo)
{
    m_repo = repo;
    m_main->setCurrentIndex(1);
    m_title->setText(repo);
    m_desc->clear();
    m_clone->clear();
    m_ref->clear();
    m_entries->clear();
    m_commits->clear();
    m_tabs->setTabText(1, "Commits");
    status("Loading " + repo + "…");
    m_api->get(repoPath(repo), {}, this, [this, repo](const QJsonObject &info, const QString &error) {
        if (repo != m_repo)
            return;
        if (!error.isEmpty())
            return status(error, true);
        const QString desc = info.value("description").toString();
        m_desc->setText(desc.isEmpty() ? QString() : desc);
        m_desc->setVisible(!desc.isEmpty());
        m_title->setText(repo + (info.value("public").toBool() ? "" : "  (private)"));
        m_clone->setText("git clone " + info.value("clone_url").toString());
        const QString def = info.value("default").toString();
        const QJsonObject branches = info.value("branches").toObject();
        const QJsonObject tags = info.value("tags").toObject();
        if (!def.isEmpty() && branches.contains(def))
            m_ref->addItem(def);
        for (const QString &b : branches.keys())
            if (b != def)
                m_ref->addItem(b);
        for (const QString &t : tags.keys())
            m_ref->addItem(t);
        if (branches.isEmpty()) {
            m_filesStack->setCurrentIndex(0);
            m_crumbs->setText("<i>Nothing has been pushed yet.</i>");
            return status("Empty repository");
        }
        const QString path = m_pendingPath;
        m_pendingPath.clear();
        browse(path);
        loadLog();
    });
}

void ForgeWindow::setBreadcrumb(const QString &path, bool isFile)
{
    const QStringList parts = path.split('/', Qt::SkipEmptyParts);
    QString html = QStringLiteral("<a href=\"/\">%1</a>").arg(m_repo.section('/', 1).toHtmlEscaped());
    QString acc;
    for (int i = 0; i < parts.size(); ++i) {
        acc += (acc.isEmpty() ? "" : "/") + parts[i];
        const bool last = i == parts.size() - 1;
        html += " / ";
        html += last ? "<b>" + parts[i].toHtmlEscaped() + "</b>"
                     : QStringLiteral("<a href=\"/%1\">%2</a>").arg(acc.toHtmlEscaped(), parts[i].toHtmlEscaped());
    }
    if (isFile)
        html += QStringLiteral("  ·  <a href=\"/%1\">back</a>").arg(parts.mid(0, parts.size() - 1).join('/').toHtmlEscaped());
    m_crumbs->setText(html);
}

void ForgeWindow::browse(const QString &path)
{
    const QString repo = m_repo;
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("path", path);
    status("Loading…");
    m_api->get(repoPath(repo) + "/tree", q, this, [this, repo](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        if (!error.isEmpty())
            return status(error, true);
        m_path = body.value("path").toString();
        const bool isFile = body.value("type").toString() == "file";
        setBreadcrumb(m_path, isFile);
        updateHash();
        status(QStringLiteral("%1 @ %2").arg(ref(), body.value("commit").toString().left(12)));
        if (isFile)
            return showFile(body);
        m_entries->clear();
        for (const auto &v : body.value("entries").toArray()) {
            const QJsonObject e = v.toObject();
            const QString name = e.value("name").toString();
            const bool dir = name.endsWith('/');
            auto *it = new QTreeWidgetItem(m_entries);
            it->setText(0, name);
            it->setIcon(0, style()->standardIcon(dir ? QStyle::SP_DirIcon : QStyle::SP_FileIcon));
            if (!e.value("size").isNull())
                it->setText(1, humanSize(e.value("size").toInteger()));
            it->setTextAlignment(1, Qt::AlignRight | Qt::AlignVCenter);
            const QString clean = dir ? name.chopped(1) : name;
            it->setData(0, PathRole, m_path.isEmpty() ? clean : m_path + "/" + clean);
            it->setData(0, DirRole, dir);
        }
        m_filesStack->setCurrentIndex(0);
    });
}

void ForgeWindow::showFile(const QJsonObject &file)
{
    if (file.value("binary").toBool())
        m_viewer->setPlainText(QStringLiteral("(binary file, %1)").arg(humanSize(file.value("size").toInteger())));
    else
        m_viewer->setPlainText(file.value("content").toString());
    m_filesStack->setCurrentIndex(1);
}

void ForgeWindow::loadLog()
{
    const QString repo = m_repo;
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("limit", "100");
    m_tabs->setTabText(1, "Commits (loading…)");
    m_api->get(repoPath(repo) + "/log", q, this, [this, repo](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        m_commits->clear();
        if (!error.isEmpty()) {
            qWarning("forge: loading commits for %s failed: %s", qPrintable(repo), qPrintable(error));
            auto *it = new QTreeWidgetItem(m_commits);
            it->setText(0, "Could not load commits: " + error + " (open this tab again to retry)");
            m_tabs->setTabText(1, "Commits (error)");
            return;
        }
        const QJsonArray commits = body.value("commits").toArray();
        if (commits.isEmpty())
            (new QTreeWidgetItem(m_commits))->setText(0, "No commits on " + ref());
        for (const auto &v : commits) {
            const QJsonObject c = v.toObject();
            const QString msg = c.value("message").toString();
            QString author = c.value("author").toString();
            author = author.section(" <", 0, 0);
            const QDateTime when = QDateTime::fromString(c.value("date").toString(), Qt::ISODate);
            auto *it = new QTreeWidgetItem(m_commits);
            it->setText(0, msg.section('\n', 0, 0));
            it->setToolTip(0, msg);
            it->setText(1, author);
            it->setText(2, QLocale().toString(when.toLocalTime(), QLocale::ShortFormat));
            it->setText(3, c.value("sha").toString().left(10));
            it->setFont(3, QFontDatabase::systemFont(QFontDatabase::FixedFont));
            it->setData(0, ShaRole, c.value("sha").toString());
        }
        m_tabs->setTabText(1, QStringLiteral("Commits (%1)").arg(commits.size()));
    });
}

void ForgeWindow::updateHash()
{
    m_api->setLocationHash(m_repo + (m_path.isEmpty() ? "" : "/" + m_path));
}
