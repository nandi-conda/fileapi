#include "forgewindow.h"
#include "api.h"
#include "appstyle.h"
#include "codeview.h"
#include "diffview.h"
#include "highlighter.h"
#include "markdownview.h"
#include "navpanel.h"
#include "secretspanel.h"
#include "releasespanel.h"

#include <QAbstractItemView>
#include <QAction>
#include <QCheckBox>
#include <QComboBox>
#include <QDateTime>
#include <QDesktopServices>
#include <QFontDatabase>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonValue>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QLocale>
#include <QPlainTextEdit>
#include <QRegularExpression>
#include <QResizeEvent>
#include <QScrollArea>
#include <QScreen>
#include <QScroller>
#include <QScrollBar>
#include <QUrl>
#include <QPushButton>
#include <QShortcut>
#include <QSignalBlocker>
#include <QTextBlock>
#include <QToolTip>
#include <QSplitter>
#include <QStackedWidget>
#include <QStatusBar>
#include <QStyle>
#include <QTabBar>
#include <QTabWidget>
#include <QTimer>
#include <QToolBar>
#include <QToolButton>
#include <QButtonGroup>
#include <QFrame>
#include <QTreeWidget>
#include <QVBoxLayout>
#include <QPointer>

#ifdef Q_OS_WASM
#include <emscripten/bind.h>
#include <emscripten/val.h>

namespace {
QPointer<ForgeWindow> g_window;  // gets the browser's popstate (back/forward, a link typed in)
void forgeRouteChanged()
{
    if (g_window)
        QMetaObject::invokeMethod(g_window.data(), [] { if (g_window) g_window->routeChanged(); }, Qt::QueuedConnection);
}
}  // namespace

EMSCRIPTEN_BINDINGS(forgeui) { emscripten::function("forgeRouteChanged", &forgeRouteChanged); }
#endif

namespace {
constexpr int PathRole = Qt::UserRole;
constexpr int DirRole = Qt::UserRole + 1;
constexpr int ShaRole = Qt::UserRole + 2;

QString humanSize(qint64 n)
{
    return QLocale::c().formattedDataSize(n, 1, QLocale::DataSizeTraditionalFormat);
}

QString repoPath(const QString &repo) { return "repos/" + repo; }

// Splits a trailing "#L12" or "#L12-L20" (GitHub-style line link) off a location hash; returns "L12…" or "".
QString splitLines(QString &hash)
{
    static const QRegularExpression re(QStringLiteral("#(L\\d+(?:-L?\\d+)?)$"));
    const auto m = re.match(hash);
    if (!m.hasMatch())
        return {};
    hash.chop(m.capturedLength());
    return m.captured(1);
}

// The repo tabs by index, as they appear in the URL (#<owner>/<repo>/-/<name>); Files has none.
const QStringList TabNames = {QString(), "commits", "releases", "settings", "deploys"};

// Splits a trailing "?ref=<ref>" off a location hash (after splitLines); returns the ref or "".
QString splitRef(QString &hash)
{
    const int at = hash.lastIndexOf(QStringLiteral("?ref="));
    if (at < 0)
        return {};
    const QString ref = hash.mid(at + 5);
    hash.truncate(at);
    return ref;
}

// "L12" / "L12-L20" / "L12-20" -> first and last line (0 when there is none).
QPair<int, int> parseLines(const QString &lines)
{
    if (lines.isEmpty())
        return {0, 0};
    const QStringList ends = lines.split('-');
    const int first = ends.value(0).mid(1).toInt();
    const QString lastStr = ends.value(1);
    const int last = lastStr.isEmpty() ? first : lastStr.mid(lastStr.startsWith('L') ? 1 : 0).toInt();
    return {first, last};
}

// The server counts columns in code points; QString indexes are UTF-16 units. They differ only after
// characters outside the BMP (emoji and the like) earlier on the line.
int toUtf16(const QString &text, int codePoints)
{
    int i = 0;
    for (int n = 0; i < text.size() && n < codePoints; ++n)
        i += (text[i].isHighSurrogate() && i + 1 < text.size()) ? 2 : 1;
    return i;
}

int toCodePoints(const QString &text, int utf16)
{
    int n = 0;
    for (int i = 0; i < utf16 && i < text.size(); ++i)
        if (!text[i].isLowSurrogate())
            ++n;
    return n;
}

// Below this width (in CSS px) the window switches to the phone layout.
constexpr int CompactWidth = 720;

}  // namespace

ForgeWindow::ForgeWindow(QWidget *parent) : QMainWindow(parent), m_api(new Api(this))
{
    setWindowTitle("fileapi forge");

    // ---- top bar
    auto *bar = addToolBar("Account");
    bar->setMovable(false);
    // Phone layout only: back from an open repo to the list of repos.
    m_backAction = bar->addAction(style()->standardIcon(QStyle::SP_ArrowBack), "Repositories");
    m_backAction->setToolTip("Back to repositories");
    m_backAction->setVisible(false);
    connect(m_backAction, &QAction::triggered, this, [this] { closeRepo(Nav::Push); });
    auto *brand = new QLabel("<b>fileapi</b> forge");
    brand->setContentsMargins(8, 0, 16, 0);
    bar->addWidget(brand);
    auto *spacer = new QWidget;
    spacer->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
    bar->addWidget(spacer);
    m_who = new QLabel;
    m_who->setContentsMargins(0, 0, 8, 0);
    m_whoAction = bar->addWidget(m_who);
    m_userSettingsBtn = new QPushButton("Settings");
    m_userSettingsBtn->setIcon(style()->standardIcon(QStyle::SP_FileDialogDetailedView));
    m_userSettingsBtn->setToolTip("Your settings: secrets for all your repos");
    m_userSettingsAct = bar->addWidget(m_userSettingsBtn);  // a toolbar widget is shown/hidden via its action
    m_userSettingsAct->setVisible(false);
    connect(m_userSettingsBtn, &QPushButton::clicked, this, [this] { showUserSettings(); });
    m_signIn = new QPushButton("Sign in");
    bar->addWidget(m_signIn);
    connect(m_signIn, &QPushButton::clicked, this, [this] {
        m_api->hasToken() ? m_api->signOut() : m_api->signIn();
    });

    // ---- repo list
    m_split = new QSplitter;
    auto *split = m_split;
    m_left = new QWidget;
    auto *left = m_left;
    auto *leftLay = new QVBoxLayout(left);
    leftLay->setContentsMargins(8, 8, 0, 8);
    leftLay->addWidget(new QLabel("<b>Repositories</b>"));
    m_repos = new QListWidget;
    touchScroll(m_repos);
    leftLay->addWidget(m_repos);
    // New repo: only a name is needed; it is created under the signed-in handle.
    m_newRow = new QWidget;
    auto *newLay = new QHBoxLayout(m_newRow);
    newLay->setContentsMargins(0, 0, 0, 0);
    m_newName = new QLineEdit;
    m_newName->setPlaceholderText("new-repo-name");
    newLay->addWidget(m_newName, 1);
    m_create = new QPushButton("Create");
    m_create->setIcon(style()->standardIcon(QStyle::SP_FileDialogNewFolder));
    newLay->addWidget(m_create);
    m_newRow->setVisible(false);
    leftLay->addWidget(m_newRow);
    connect(m_create, &QPushButton::clicked, this, &ForgeWindow::createRepo);
    connect(m_newName, &QLineEdit::returnPressed, this, &ForgeWindow::createRepo);
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
    m_repoLayout = lay;
    lay->setContentsMargins(8, 8, 8, 8);
    m_title = new QLabel;
    QFont tf = m_title->font();
    tf.setPointSizeF(tf.pointSizeF() * 1.5);
    tf.setBold(true);
    m_title->setFont(tf);
    m_title->setTextInteractionFlags(Qt::TextSelectableByMouse);
    // The header keeps one fixed size while a repo loads, so nothing below it jumps: the description
    // always takes exactly one line (cut off when long, full text in the tooltip), even when empty.
    m_title->setFixedHeight(QFontMetrics(tf).lineSpacing() + 4);
    m_title->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    m_titleRow = new QHBoxLayout;
    m_titleRow->addWidget(m_title, 1);
    lay->addLayout(m_titleRow);
    m_desc = new QLabel;
    m_desc->setWordWrap(false);
    m_desc->setFixedHeight(m_desc->fontMetrics().lineSpacing() + 2);
    m_desc->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    lay->addWidget(m_desc);

    // Branch and clone URL side by side; stacked in the phone layout.
    m_headRow = new QBoxLayout(QBoxLayout::LeftToRight);
    m_branchRow = new QHBoxLayout;
    auto *branchRow = m_branchRow;
    m_branchLabel = new QLabel("Branch:");
    branchRow->addWidget(m_branchLabel);
    m_ref = new QComboBox;
    m_ref->setMinimumWidth(160);
    m_ref->setEditable(true);  // also accepts a commit sha or tag typed in
    branchRow->addWidget(m_ref, 1);
    m_headRow->addLayout(branchRow);
    m_headRow->addSpacing(16);
    m_cloneRow = new QWidget;
    auto *cloneRow = new QHBoxLayout(m_cloneRow);
    cloneRow->setContentsMargins(0, 0, 0, 0);
    auto *cloneLabel = new QLabel("Clone:");
    cloneLabel->setMinimumWidth(m_branchLabel->sizeHint().width());
    cloneRow->addWidget(cloneLabel);
    m_clone = new QLineEdit;
    m_clone->setReadOnly(true);
    cloneRow->addWidget(m_clone, 1);
    m_headRow->addWidget(m_cloneRow, 1);
    lay->addLayout(m_headRow);
    connect(m_ref, &QComboBox::textActivated, this, [this] {
        closeNav();
        m_commitStack->setCurrentIndex(0);
        if (m_tabs->currentIndex() == 0)
            browse(QString(), Nav::Push);
        else {
            browse(QString(), Nav::Replace);
            updateHash(Nav::Push);  // e.g. #<repo>/-/commits?ref=<branch>
        }
        loadLog();
    });

    m_tabs = new QTabWidget;
    // Wide enough for "Commits (100)", so the tabs don't shift when the commit count arrives.
    m_tabs->tabBar()->setStyleSheet(QStringLiteral("QTabBar::tab { min-width: %1px; }")
                                        .arg(m_tabs->fontMetrics().horizontalAdvance("Commits (100)") + 8));
    // Files tab: breadcrumb, then either a directory listing or a file viewer.
    auto *files = new QWidget;
    auto *fl = new QVBoxLayout(files);
    fl->setContentsMargins(4, 4, 4, 4);
    m_crumbs = new QLabel;
    m_crumbs->setTextFormat(Qt::RichText);
    m_crumbs->setFixedHeight(m_crumbs->fontMetrics().lineSpacing() + 6);
    m_crumbs->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    connect(m_crumbs, &QLabel::linkActivated, this, [this](const QString &link) { browse(link.mid(1)); });
    m_crumbRow = new QBoxLayout(QBoxLayout::LeftToRight);
    auto *crumbRow = m_crumbRow;
    crumbRow->addWidget(m_crumbs, 1);
    // Markdown files: the rendered page or its source, like GitHub's Preview / Code.
    m_viewToggle = new QWidget;
    auto *toggleRow = new QHBoxLayout(m_viewToggle);
    toggleRow->setContentsMargins(0, 0, 0, 0);
    toggleRow->setSpacing(0);
    m_previewBtn = new QToolButton;
    m_previewBtn->setText("Preview");
    m_codeBtn = new QToolButton;
    m_codeBtn->setText("Code");
    auto *toggleGroup = new QButtonGroup(m_viewToggle);
    for (QToolButton *b : {m_previewBtn, m_codeBtn}) {
        b->setCheckable(true);
        b->setAutoRaise(false);
        b->setToolButtonStyle(Qt::ToolButtonTextOnly);
        toggleGroup->addButton(b);
        toggleRow->addWidget(b);
    }
    m_viewToggle->setVisible(false);
    crumbRow->addWidget(m_viewToggle, 0, Qt::AlignRight);
    connect(m_previewBtn, &QToolButton::clicked, this, [this] { setPreview(true); });
    connect(m_codeBtn, &QToolButton::clicked, this, [this] { setPreview(false); });
    // The open file's outline: jump to any class/function in it.
    m_outline = new QComboBox;
    m_outline->setMinimumWidth(220);
    m_outline->setMaximumWidth(360);
    m_outline->setToolTip("Symbols in this file");
    m_outline->setVisible(false);
    crumbRow->addWidget(m_outline);
    fl->addLayout(crumbRow);
    connect(m_outline, &QComboBox::activated, this, [this](int i) {
        const QJsonObject d = m_outline->itemData(i).toJsonObject();
        if (!d.isEmpty())
            openPlace(m_path, d.value("line").toInt(), d.value("col").toInt(), d.value("end_col").toInt());
        const QSignalBlocker block(m_outline);
        m_outline->setCurrentIndex(0);
    });
    m_filesStack = new QStackedWidget;
    m_entries = new QTreeWidget;
    m_entries->setHeaderLabels({"Name", "Size"});
    m_entries->setRootIsDecorated(false);
    m_entries->setUniformRowHeights(true);
    m_entries->setProperty("singleClick", true);  // opens on a single click, like links on the web
    m_entries->setCursor(Qt::PointingHandCursor);
    // The list is as tall as its rows and scrolls together with the README under it.
    m_entries->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_entries->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_entries->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);
    m_entries->header()->setStretchLastSection(false);
    m_entries->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_entries->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    connect(m_entries, &QTreeWidget::itemActivated, this, [this](QTreeWidgetItem *it) {
        browse(it->data(0, PathRole).toString());
    });
    m_listScroll = new QScrollArea;
    m_listScroll->setWidgetResizable(true);
    m_listScroll->setFrameShape(QFrame::NoFrame);
    m_listPage = new QWidget;
    auto *lp = new QVBoxLayout(m_listPage);
    lp->setContentsMargins(0, 0, 0, 0);
    lp->setSpacing(12);
    lp->addWidget(m_entries);
    auto *readmeFrame = new QFrame;
    readmeFrame->setFrameShape(QFrame::StyledPanel);
    m_readmeBox = readmeFrame;
    auto *rl = new QVBoxLayout(m_readmeBox);
    rl->setContentsMargins(1, 1, 1, 1);
    rl->setSpacing(0);
    m_readmeTitle = new QLabel;
    m_readmeTitle->setContentsMargins(12, 8, 12, 8);
    QFont rtf = m_readmeTitle->font();
    rtf.setBold(true);
    m_readmeTitle->setFont(rtf);
    rl->addWidget(m_readmeTitle);
    auto *rule = new QFrame;
    rule->setFrameShape(QFrame::HLine);
    rule->setFrameShadow(QFrame::Plain);
    rule->setForegroundRole(QPalette::Mid);
    rl->addWidget(rule);
    m_readme = new MarkdownView(m_api);
    m_readme->setFrameShape(QFrame::NoFrame);
    m_readme->setAutoHeight(true);
    if (isTouchUi())
        m_readme->setTextInteractionFlags(Qt::LinksAccessibleByMouse);  // a drag scrolls, a tap follows links
    m_readme->onOpenPath = [this](const QString &path, const QString &frag) { openRepoLink(path, frag); };
    m_readme->onScrollTo = [this](int y) {
        m_listScroll->verticalScrollBar()->setValue(m_readme->mapTo(m_listPage, QPoint(0, y)).y());
    };
    rl->addWidget(m_readme);
    m_readmeBox->setVisible(false);
    lp->addWidget(m_readmeBox);
    lp->addStretch(1);
    m_listScroll->setWidget(m_listPage);
    touchScroll(m_listScroll);
    m_filesStack->addWidget(m_listScroll);
    m_viewer = new CodeView;
    m_viewer->onLinesClicked = [this](int, int) { updateHash(); };
    m_viewer->setReadOnly(true);
    m_viewer->setLineWrapMode(QPlainTextEdit::NoWrap);
    m_viewer->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
    m_highlighter = new CodeHighlighter(m_viewer->document());
    if (isTouchUi()) {
        // A finger drag scrolls the code instead of selecting it, and there's no hover to wait for.
        m_viewer->setTextInteractionFlags(Qt::TextSelectableByKeyboard);
        m_viewer->setHoverEnabled(false);
        m_viewer->setCursorWidth(0);  // no caret: there's nothing to type
        touchScroll(m_viewer);
    }
    m_viewer->onWordClicked = [this](int line, int col, bool jump) { lookupSymbol(line, col, jump); };
    m_viewer->onWordHover = [this](int line, int col, const QPoint &at) { hoverSymbol(line, col, at); };
    m_viewer->onNavKey = [this](int line, int col, bool refs) { lookupSymbol(line, col, !refs); };
    m_fileSplit = new QSplitter;
    m_codeStack = new QStackedWidget;
    m_codeStack->addWidget(m_viewer);
    m_mdView = new MarkdownView(m_api);
    m_mdView->onOpenPath = [this](const QString &path, const QString &frag) { openRepoLink(path, frag); };
    if (isTouchUi())
        m_mdView->setTextInteractionFlags(Qt::LinksAccessibleByMouse);
    touchScroll(m_mdView);
    m_codeStack->addWidget(m_mdView);
    m_fileSplit->addWidget(m_codeStack);
    m_navPanel = new NavPanel;
    m_navPanel->setVisible(false);
    touchScroll(m_navPanel->view());
    m_navPanel->onOpen = [this](const QString &path, int line, int col, int end) { openPlace(path, line, col, end); };
    m_navPanel->onClose = [this] { closeNav(); };
    m_navPanel->onBack = [this] { goBack(); };
    m_fileSplit->addWidget(m_navPanel);
    m_fileSplit->setStretchFactor(0, 3);
    m_fileSplit->setStretchFactor(1, 1);
    m_filesStack->addWidget(m_fileSplit);
    auto *back = new QShortcut(QKeySequence(Qt::ALT | Qt::Key_Left), this);
    connect(back, &QShortcut::activated, this, [this] { goBack(); });
    auto *esc = new QShortcut(QKeySequence(Qt::Key_Escape), m_fileSplit);
    connect(esc, &QShortcut::activated, this, [this] { closeNav(); });
    // Highlight colours follow the viewer: dark text-on-light or light-on-dark.
    syncHighlightTheme();  // and again on every palette change, see changeEvent()
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
    m_commits->setToolTip("Click a commit to see its changes.");
    m_commits->setProperty("singleClick", true);
    touchScroll(m_commits);
    connect(m_commits, &QTreeWidget::itemActivated, this, [this](QTreeWidgetItem *it) {
        const QString sha = it->data(0, ShaRole).toString();
        if (!sha.isEmpty())
            showCommit(sha, Nav::Push);
    });
    // The Commits tab: the list, or one commit's diff (#<repo>/-/commits/<sha>).
    m_commitStack = new QStackedWidget;
    m_commitStack->addWidget(m_commits);
    m_commitView = new CommitView(m_api);
    connect(m_commitView, &CommitView::back, this, [this] {
        m_commitStack->setCurrentIndex(0);
        updateHash(Nav::Push);
    });
    connect(m_commitView, &CommitView::openCommit, this, [this](const QString &sha) { showCommit(sha, Nav::Push); });
    connect(m_commitView, &CommitView::statusText, this, [this](const QString &t, bool e) { status(t, e); });
    connect(m_commitView, &CommitView::browseFiles, this, [this](const QString &sha) {
        m_ref->setCurrentText(sha.left(12));
        m_commitStack->setCurrentIndex(0);
        m_routing = true;  // one history entry for the whole move, made by browse
        m_tabs->setCurrentIndex(0);
        m_routing = false;
        browse(QString(), Nav::Push);
        loadLog();
    });
    m_commitStack->addWidget(m_commitView);
    m_tabs->addTab(m_commitStack, "Commits");
    // Releases: everyone who can see the repo can download; the owner makes them and uploads files.
    m_releases = new ReleasesPanel(m_api);
    connect(m_releases, &ReleasesPanel::statusText, this, [this](const QString &t, bool e) { status(t, e); });
    connect(m_releases, &ReleasesPanel::tagPicked, this, [this] { updateHash(Nav::Push); });
    auto *releasesScroll = new QScrollArea;  // a phone's stacked buttons can run past the screen
    releasesScroll->setWidget(m_releases);
    releasesScroll->setWidgetResizable(true);
    releasesScroll->setFrameShape(QFrame::NoFrame);
    touchScroll(releasesScroll);
    m_tabs->addTab(releasesScroll, "Releases");
    connect(m_tabs, &QTabWidget::currentChanged, this, [this](int i) {
        if (i == 2 && !m_repo.isEmpty() && !m_releases->loaded())
            m_releases->load();
    });
    connect(m_tabs, &QTabWidget::currentChanged, this, [this](int i) {
        if (i == 1 && !m_repo.isEmpty() && m_ref->count() > 0
            && (m_commits->topLevelItemCount() == 0 || m_commitsBadge == "error"))
            loadLog();
    });
    m_settings = buildSettings();
    // Scrolls when the screen is shorter than the page (phones, or with the on-screen keyboard up).
    auto *settingsScroll = new QScrollArea;
    settingsScroll->setWidget(m_settings);
    settingsScroll->setWidgetResizable(true);
    settingsScroll->setFrameShape(QFrame::NoFrame);
    touchScroll(settingsScroll);
    m_tabs->addTab(settingsScroll, "Settings");
    m_tabs->setTabVisible(3, false);  // only the signed-in admin can change settings
    m_deploys = buildDeploys();
    m_tabs->addTab(m_deploys, "Deploys");
    m_tabs->setTabVisible(4, false);
    connect(m_tabs, &QTabWidget::currentChanged, this, [this](int i) {
        if (i == 4 && !m_repo.isEmpty())
            loadRuns();
    });
    // Every tab has its own URL, and switching tabs is a step Back undoes. (When code switches tabs
    // with m_routing set, it updates the URL itself.)
    connect(m_tabs, &QTabWidget::currentChanged, this, [this] {
        if (!m_routing && !m_repo.isEmpty() && m_main->currentIndex() == 1)
            updateHash(Nav::Push);
    });
    lay->addWidget(m_tabs, 1);
    m_main->addWidget(repoView);

    // ---- error page: a repo that can't be opened. For anyone not signed in, a private repo and a missing
    // one look the same (the server answers 404 for both), so the page never says which it is.
    auto *errPage = new QWidget;
    auto *el = new QVBoxLayout(errPage);
    el->addStretch(1);
    m_errIcon = new QLabel;
    m_errIcon->setAlignment(Qt::AlignCenter);
    el->addWidget(m_errIcon);
    m_errTitle = new QLabel;
    QFont ef = m_errTitle->font();
    ef.setPointSizeF(ef.pointSizeF() * 1.5);
    ef.setBold(true);
    m_errTitle->setFont(ef);
    m_errTitle->setAlignment(Qt::AlignCenter);
    el->addWidget(m_errTitle);
    m_errText = new QLabel;
    m_errText->setWordWrap(true);
    m_errText->setAlignment(Qt::AlignCenter);
    m_errText->setTextInteractionFlags(Qt::TextSelectableByMouse);
    el->addWidget(m_errText);
    el->addSpacing(12);
    m_errButtons = new QBoxLayout(QBoxLayout::LeftToRight);
    auto *eb = m_errButtons;
    eb->addStretch(1);
    m_errSignIn = new QPushButton("Sign in");
    eb->addWidget(m_errSignIn);
    m_errRetry = new QPushButton("Try again");
    m_errRetry->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    eb->addWidget(m_errRetry);
    auto *errBack = new QPushButton("Back to repositories");
    errBack->setIcon(style()->standardIcon(QStyle::SP_ArrowBack));
    eb->addWidget(errBack);
    eb->addStretch(1);
    el->addLayout(eb);
    el->addStretch(2);
    connect(m_errSignIn, &QPushButton::clicked, this, [this] { m_api->signIn(); });  // keeps #<repo>, comes back to it
    connect(m_errRetry, &QPushButton::clicked, this, [this] {
        const QString repo = m_repo;
        m_repo.clear();
        openRepo(repo, Nav::Replace);
    });
    connect(errBack, &QPushButton::clicked, this, [this] { closeRepo(Nav::Push); });
    m_main->addWidget(errPage);

    // ---- user settings (page 3): things that apply to every repo the signed-in account owns
    auto *userPage = new QWidget;
    auto *ul = new QVBoxLayout(userPage);
    ul->setContentsMargins(8, 8, 8, 8);
    auto *userTitle = new QLabel("Your settings");
    QFont utf = userTitle->font();
    utf.setPointSizeF(utf.pointSizeF() * 1.5);
    utf.setBold(true);
    userTitle->setFont(utf);
    ul->addWidget(userTitle);
    m_userSecrets = new SecretsPanel(m_api, "User secrets",
        "Every repo you own gets these in its deploy jobs (<code>.forge/jobs.toml</code>), as environment variables "
        "and <code>${NAME}</code>: set a token like <code>DEPOT_TOKEN</code> once instead of per repo. A repo "
        "secret of the same name (repo Settings tab) overrides one for that repo. Encrypted on the server and "
        "never shown again.");
    m_userSecrets->setPath("user/secrets");
    connect(m_userSecrets, &SecretsPanel::statusText, this, &ForgeWindow::status);
    ul->addWidget(m_userSecrets);
    ul->addStretch(1);
    m_main->addWidget(userPage);
    split->addWidget(m_main);
    split->setStretchFactor(1, 1);
    split->setSizes({260, 900});
    setCentralWidget(split);

    m_status = new QLabel;
    m_status->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);  // long messages never widen the window
    statusBar()->addWidget(m_status, 1);
    statusBar()->setSizeGripEnabled(false);
    connect(m_main, &QStackedWidget::currentChanged, this, [this] { updatePanes(); });
    if (isTouchUi())
        m_commits->setToolTip(QString());

    if (screen())
        setCompact(screen()->geometry().width() < CompactWidth);  // before the first paint, so it doesn't jump
#ifdef Q_OS_WASM
    g_window = this;
    emscripten::val::global("window").call<void>("addEventListener", std::string("popstate"),
                                                 emscripten::val::module_property("forgeRouteChanged"));
#endif
    loadMe();
    loadRepos();
}

void ForgeWindow::routeChanged()
{
    applyRoute(m_api->locationHash());
}

void ForgeWindow::closeRepo(Nav nav)
{
    m_repo.clear();
    m_pendingPath.clear();
    m_hasPendingTab = false;
    m_api->setLocationHash(QString(), nav == Nav::Push);
    const QSignalBlocker block(m_repos);
    m_repos->setCurrentRow(-1);
    m_main->setCurrentIndex(0);
    status(QStringLiteral("%1 repositories").arg(m_repos->count()));
}

void ForgeWindow::resizeEvent(QResizeEvent *event)
{
    QMainWindow::resizeEvent(event);
    // The window fills the page (the browser viewport, which is the "screen"), unless the desktop layout's
    // minimum width pushed it wider: decide by the screen, then fit the window back onto it.
    const QRect screenRect = screen() ? screen()->geometry() : QRect(QPoint(), event->size());
    setCompact(std::min(event->size().width(), screenRect.width()) < CompactWidth);
    if (event->size().width() > screenRect.width())
        QTimer::singleShot(0, this, [this] {
            if (!screen() || width() <= screen()->geometry().width())
                return;
            // Let the phone layout's smaller minimum size take effect first, or the window can't shrink.
            QCoreApplication::sendPostedEvents(nullptr, QEvent::LayoutRequest);
            if (layout())
                layout()->activate();
            setGeometry(screen()->geometry());
        });
}

void ForgeWindow::updatePanes()
{
    // Phone layout: the repo list fills the screen until a repo opens; then the repo does, with Back.
    const bool listOnly = m_compact && m_main->currentIndex() == 0;
    m_left->setVisible(!m_compact || listOnly);
    m_main->setVisible(!listOnly);
    m_backAction->setVisible(m_compact && !listOnly);
}

void ForgeWindow::setCompact(bool compact)
{
    if (compact == m_compact && m_split->property("laidOut").toBool())
        return;
    m_split->setProperty("laidOut", true);
    m_compact = compact;
    m_whoAction->setVisible(!compact);
    m_userSettingsBtn->setText(compact ? QString() : "Settings");  // its icon alone on a phone  // "Signed in as …": the Sign in/out button says enough
    m_left->layout()->setContentsMargins(8, 8, compact ? 8 : 0, 8);
    m_headRow->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
    m_headRow->itemAt(1)->spacerItem()->changeSize(compact ? 0 : 16, compact ? 0 : 0);
    // Phone: the branch picker moves up beside the repo name, saving a row for the code.
    m_ref->setMinimumWidth(compact ? 0 : 160);
    m_ref->setMaximumWidth(compact ? 150 : QWIDGETSIZE_MAX);
    if (compact && m_titleRow->indexOf(m_ref) < 0) {
        m_branchRow->removeWidget(m_ref);
        m_titleRow->addWidget(m_ref);
    } else if (!compact && m_branchRow->indexOf(m_ref) < 0) {
        m_titleRow->removeWidget(m_ref);
        m_branchRow->addWidget(m_ref, 1);
    }
    m_branchLabel->setVisible(!compact);
    m_crumbRow->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
    m_outline->setMinimumWidth(compact ? 0 : 220);
    m_outline->setMaximumWidth(compact ? QWIDGETSIZE_MAX : 360);
    // Code and the definitions/references panel: side by side, or the panel under the code.
    m_fileSplit->setOrientation(compact ? Qt::Vertical : Qt::Horizontal);
    m_commits->setColumnHidden(1, compact);  // author
    m_commits->setColumnHidden(3, compact);  // sha
    m_commitView->setCompact(compact);
    for (int c : {2, 3, 4, 6})              // branch, commit, started, trigger (the Depot link stays)
        m_runs->setColumnHidden(c, compact);
    m_repoSecrets->setCompact(compact);
    m_releases->setCompact(compact);
    m_userSecrets->setCompact(compact);
    m_settingsForm->setRowWrapPolicy(compact ? QFormLayout::WrapAllRows : QFormLayout::DontWrapRows);
    m_errButtons->setDirection(compact ? QBoxLayout::TopToBottom : QBoxLayout::LeftToRight);
    m_runsNote->setVisible(!compact);
    m_runBranch->setMinimumWidth(compact ? 80 : 140);
    m_runBranchLabel->setVisible(!compact);
    m_runRefresh->setText(compact ? QString() : "Refresh");  // the icon alone on a phone
    m_cloneRow->setVisible(!compact);
    m_desc->setVisible(!compact || m_path.isEmpty());  // a phone has no git to paste it into; Settings and desktop still show it
    m_repoLayout->setContentsMargins(compact ? 4 : 8, compact ? 4 : 8, compact ? 4 : 8, compact ? 4 : 8);
    QFont tf = font();
    tf.setPointSizeF(tf.pointSizeF() * (compact ? 1.2 : 1.5));
    tf.setBold(true);
    m_title->setFont(tf);
    m_title->setFixedHeight(QFontMetrics(tf).lineSpacing() + 4);
    m_tabs->tabBar()->setUsesScrollButtons(true);  // never wider than the screen, whatever the labels
    setTabBadge(1, m_commitsBadge);
    setTabBadge(4, m_deploysBadge);
    const QFontMetrics fm = m_tabs->fontMetrics();
    // Wide enough for "Commits (100)", so the tabs don't shift when the commit count arrives; on a phone
    // the four tabs have to share the width instead.
    m_tabs->tabBar()->setStyleSheet(QStringLiteral("QTabBar::tab { min-width: %1px; }")
                                        .arg(compact ? 0 : fm.horizontalAdvance("Commits (100)") + 8));
    m_tabs->tabBar()->setExpanding(compact);
    // Widgets on hidden tabs don't re-measure their rows on their own, and a stale (desktop-sized) minimum
    // would keep the window wider than a phone: recompute every layout now.
    for (QLayout *l : findChildren<QLayout *>())
        l->invalidate();
    // And whatever a page off screen would like, the window never gets wider than the phone: the stacks'
    // other pages (and anything not yet re-measured) don't get to push it.
    m_split->setSizePolicy(compact ? QSizePolicy::Ignored : QSizePolicy::Preferred, QSizePolicy::Preferred);
    if (compact)
        m_split->setSizes({0, 1});
    else
        m_split->setSizes({260, std::max(400, width() - 260)});
    updatePanes();
}

void ForgeWindow::setTabBadge(int tab, const QString &badge)
{
    // "Commits (40)", "Deploys (running)"; on a phone only an error or a running deploy is worth the width.
    (tab == 1 ? m_commitsBadge : m_deploysBadge) = badge;
    const QString name = tab == 1 ? "Commits" : "Deploys";
    const bool show = !badge.isEmpty() && (!m_compact || badge == "error" || badge == "running");
    m_tabs->setTabText(tab, show ? QStringLiteral("%1 (%2)").arg(name, m_compact && badge == "running" ? "…" : badge) : name);
}

void ForgeWindow::status(const QString &text, bool error)
{
    m_status->setText(text);
    m_status->setStyleSheet(error ? "color: #c62828;" : "");
}

void ForgeWindow::showRepoError(const QString &repo, int httpStatus, const QString &error)
{
    const bool noAccess = httpStatus == 401 || httpStatus == 403 || httpStatus == 404;
    // A repo name that isn't valid at all (400/422) is just as "not there" as a missing one.
    const bool missing = noAccess || httpStatus == 400 || httpStatus == 422;
    m_lastError = {{"repo", repo}, {"status", httpStatus}, {"error", error}};
    m_main->setCurrentIndex(2);
    m_errIcon->setPixmap(style()->standardIcon(missing ? QStyle::SP_MessageBoxWarning : QStyle::SP_MessageBoxCritical)
                             .pixmap(48, 48));
    if (missing) {
        m_errTitle->setText("Repository not found");
        m_errText->setText(m_signedIn
            ? QStringLiteral("<b>%1</b> doesn't exist. It may have been deleted, or the link may be mistyped.")
                  .arg(repo.toHtmlEscaped())
            : QStringLiteral("<b>%1</b> doesn't exist, or it's private and you need to sign in to see it.")
                  .arg(repo.toHtmlEscaped()));
    } else {
        m_errTitle->setText("Couldn't load this repository");
        m_errText->setText(QStringLiteral("Something went wrong loading <b>%1</b>: %2")
                               .arg(repo.toHtmlEscaped(), error.toHtmlEscaped()));
    }
    m_errSignIn->setVisible(missing && !m_signedIn);
    m_errRetry->setVisible(!missing);
    status(missing ? "Not found: " + repo : error, true);
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
        m_owner = me.value("owner").toString();
        m_who->setText(in ? "Signed in as " + (m_owner.isEmpty() ? admin : m_owner) : "Public repos only");
        m_signIn->setText(in ? "Sign out" : "Sign in");
        m_signIn->setToolTip(in ? QString() : "Only " + admin + " can sign in, to see private repos.");
        m_signedIn = in;
        m_meKnown = true;
        m_tabs->setTabVisible(3, in);
        m_tabs->setTabVisible(4, in);
        if (m_hasPendingTab && m_main->currentIndex() == 1 && !m_info.isEmpty())
            applyPendingTab();  // a link to Settings or Deploys waited to learn whether those exist
        if (m_releases->loaded())
            m_releases->load();  // signing in shows drafts and the edit buttons
        m_newRow->setVisible(in);
        m_userSettingsAct->setVisible(in);
        if (in && m_api->locationHash() == "settings")
            showUserSettings(Nav::Replace);
        m_newName->setToolTip(QStringLiteral("Creates %1/<name>").arg(m_owner));
        if (m_ownerPrefix)
            m_ownerPrefix->setText(m_owner + " /");
        if (m_main->currentIndex() == 2 && !m_lastError.isEmpty())  // page drawn before we knew: redo the wording
            showRepoError(m_lastError.value("repo").toString(), m_lastError.value("status").toInt(),
                          m_lastError.value("error").toString());
        if (m_repos->count() > 0)
            loadRepos();  // relabel with short names now that we know whose repos they are
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
            auto *it = new QListWidgetItem(shortName(name) + (pub ? "" : "  (private)"));
            const QString desc = r.value("description").toString();
            if (isTouchUi() && !desc.isEmpty())  // no hover tooltips on a phone: show it under the name
                it->setText(it->text() + "\n" + desc);
            it->setData(PathRole, name);
            it->setToolTip(r.value("description").toString());
            m_repos->addItem(it);
        }
        status(repos.isEmpty() ? "No repositories you can see yet." : QStringLiteral("%1 repositories").arg(repos.size()));
        // Restore the view the page URL names. A repo that's already open (the list reloaded after
        // sign-in) is just reselected: its view is what the URL says already.
        QString hash = m_api->locationHash();
        const QStringList parts = hash.split('/', Qt::SkipEmptyParts);
        const QString repo = parts.size() >= 2 ? parts[0] + "/" + parts[1].section('?', 0, 0).section('#', 0, 0)
                                               : QString();
        if (!repo.isEmpty() && repo == m_repo) {
            const QSignalBlocker block(m_repos);
            for (int i = 0; i < m_repos->count(); ++i)
                if (m_repos->item(i)->data(PathRole).toString() == repo)
                    m_repos->setCurrentRow(i);
        } else if (!repo.isEmpty()) {
            applyRoute(hash);
        }
    });
}

QString ForgeWindow::defaultRef() const
{
    const QString def = m_info.value("default").toString();
    return def.isEmpty() ? m_ref->itemText(0) : def;
}

void ForgeWindow::applyRoute(QString hash)
{
    if (hash == "settings") {
        if (m_signedIn && m_main->currentIndex() != 3)
            showUserSettings(Nav::Replace);
        return;  // not signed in (yet): loadMe opens it once it knows
    }
    const QString lines = splitLines(hash);
    const QString routeRef = splitRef(hash);
    const QStringList parts = hash.split('/', Qt::SkipEmptyParts);
    if (parts.size() < 2) {
        if (m_main->currentIndex() != 0)
            closeRepo(Nav::Replace);
        return;
    }
    const QString repo = parts[0] + "/" + parts[1];
    QStringList rest = parts.mid(2);
    int tab = 0;
    QString item;
    if (rest.value(0) == "-" && rest.size() >= 2) {
        tab = std::max(0, int(TabNames.indexOf(rest[1].toLower())));
        item = rest.mid(2).join('/');
        rest.clear();
    }
    const QString path = rest.join('/');
    m_hasPendingTab = true;
    m_pendingTab = tab;
    m_pendingItem = item;

    if (repo != m_repo || m_main->currentIndex() != 1) {
        m_pendingPath = path;
        m_pendingLines = lines;
        m_pendingRef = routeRef;
        {
            const QSignalBlocker block(m_repos);
            m_repos->setCurrentRow(-1);
            for (int i = 0; i < m_repos->count(); ++i)
                if (m_repos->item(i)->data(PathRole).toString() == repo)
                    m_repos->setCurrentRow(i);
        }
        // An old name (from before repos moved under their owner, or before a rename) opens too: the
        // server resolves it, and openRepo switches to the current one.
        openRepo(repo, Nav::Replace);
        return;
    }
    if (m_info.isEmpty())
        return;  // still loading: the pending view applies once it has

    // The repo is open: change only what differs.
    const QString wantRef = routeRef.isEmpty() ? defaultRef() : routeRef;
    const bool refChanged = m_ref->count() > 0 && wantRef != ref();
    if (refChanged) {
        m_ref->setCurrentText(wantRef);
        closeNav();
        loadLog();
    }
    applyPendingTab();
    if (tab == 0) {
        const auto [first, last] = parseLines(lines);
        if (refChanged || path != m_path) {
            m_pendingLines = lines;
            browse(path, Nav::Replace);
        } else if (m_filesStack->currentIndex() == 1
                   && (first != m_viewer->markedFirst() || std::max(first, last) != m_viewer->markedLast())) {
            if (first > 0 && m_viewToggle->isVisible())
                setPreview(false);
            m_viewer->setMarkedLines(first, last, first > 0);
        }
    } else if (refChanged) {
        browse(QString(), Nav::Replace);
    }
}

void ForgeWindow::applyPendingTab()
{
    int tab = m_pendingTab;
    if ((tab == 3 || tab == 4) && !m_tabs->isTabVisible(tab)) {
        if (!m_meKnown)
            return;  // loadMe calls back once it knows whether these tabs exist
        tab = 0;     // not signed in: there's no such tab
    }
    m_hasPendingTab = false;
    const QString item = std::exchange(m_pendingItem, QString());
    if (tab == 1) {
        m_pendingCommit = item;
        selectPendingCommit();
        if (!item.isEmpty())
            m_commitView->showCommit(item);
        m_commitStack->setCurrentIndex(item.isEmpty() ? 0 : 1);
    } else if (tab == 2) {
        if (item != m_releases->pickedTag())
            m_releases->selectTag(item);
    } else if (tab == 4 && item != m_runShown) {
        m_runShown = item;
        m_runOutput->clear();
        if (m_tabs->currentIndex() == 4)
            loadRuns();  // otherwise the tab switch below loads them
    }
    m_routing = true;
    m_tabs->setCurrentIndex(tab);
    m_routing = false;
    updateHash(Nav::Replace);
}

void ForgeWindow::showCommit(const QString &sha, Nav nav)
{
    m_commitView->showCommit(sha);
    m_commitStack->setCurrentIndex(1);
    m_pendingCommit = sha;
    selectPendingCommit();  // so Back to the list lands on it
    updateHash(nav);
}

void ForgeWindow::selectPendingCommit()
{
    if (m_pendingCommit.isEmpty()) {
        const QSignalBlocker block(m_commits);
        m_commits->clearSelection();
        return;
    }
    for (int i = 0; i < m_commits->topLevelItemCount(); ++i) {
        QTreeWidgetItem *it = m_commits->topLevelItem(i);
        if (it->data(0, ShaRole).toString().startsWith(m_pendingCommit)) {
            const QSignalBlocker block(m_commits);
            m_commits->setCurrentItem(it);
            m_commits->scrollToItem(it, QAbstractItemView::PositionAtCenter);
            return;  // keep m_pendingCommit, so the URL keeps naming it until something else is picked
        }
    }
}

void ForgeWindow::openRepo(const QString &repo, Nav nav)
{
    if (nav == Nav::Push) {  // picked from the list: not where some URL pointed
        m_hasPendingTab = false;
        m_pendingRef.clear();
        m_pendingCommit.clear();
    }
    m_repo = repo;
    m_main->setCurrentIndex(1);
    m_title->setText(repo);
    m_desc->clear();
    m_desc->setToolTip(QString());
    m_crumbs->setText(QStringLiteral("<b>%1</b>").arg(repo.section('/', 1).toHtmlEscaped()));
    m_clone->clear();
    m_ref->clear();
    m_path.clear();
    m_entries->clear();
    {
        const QSignalBlocker block(m_commits);
        m_commits->clear();
    }
    m_commitView->setRepo(repo);
    m_commitStack->setCurrentIndex(0);
    setTabBadge(1, QString());
    m_info = {};
    m_backStack.clear();
    m_navPanel->setBackEnabled(false);
    closeNav();
    m_settings->setEnabled(false);
    m_repoSecrets->setPath(repoPath(repo) + "/secrets");
    m_releases->setRepo(repo);
    if (m_tabs->currentIndex() == 2)
        m_releases->load();
    m_runs->clear();
    m_runOutput->clear();
    m_runShown.clear();
    m_runBranch->clear();
    status("Loading " + repo + "…");
    m_api->get(repoPath(repo), {}, this, [this, repo, nav](const QJsonObject &info, const QString &error) {
        if (repo != m_repo)
            return;
        if (!error.isEmpty())
            return showRepoError(repo, info.value(Api::StatusKey).toInt(), error);
        const QString current = info.value("repo").toString();
        if (!current.isEmpty() && current != repo) {  // opened by an old name
            m_repo = current;
            const QSignalBlocker block(m_repos);
            for (int i = 0; i < m_repos->count(); ++i)
                if (m_repos->item(i)->data(PathRole).toString() == current)
                    m_repos->setCurrentRow(i);
        }
        m_info = info;
        fillSettings(info);
        const QString desc = info.value("description").toString();
        m_desc->setText(desc);
        m_desc->setToolTip(desc);
        m_title->setText(m_repo + (info.value("public").toBool() ? "" : "  (private)"));
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
        if (const QString r = std::exchange(m_pendingRef, QString()); !r.isEmpty())
            m_ref->setCurrentText(r);
        if (m_hasPendingTab)
            applyPendingTab();
        if (branches.isEmpty()) {
            m_filesStack->setCurrentIndex(0);
            m_crumbs->setText("<i>Nothing has been pushed yet.</i>");
            m_pendingPath.clear();
            updateHash(nav);
            return status("Empty repository");
        }
        const QString path = m_pendingPath;
        m_pendingPath.clear();
        if (m_tabs->currentIndex() != 0)
            updateHash(nav);  // the tab's URL; browse below only rewrites it
        browse(path, m_tabs->currentIndex() == 0 ? nav : Nav::Replace);
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

void ForgeWindow::browse(const QString &path, Nav nav)
{
    const QString repo = m_repo;
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("path", path);
    status("Loading…");
    m_api->get(repoPath(repo) + "/tree", q, this, [this, repo, nav](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        if (!error.isEmpty())
            return status(error, true);
        m_path = body.value("path").toString();
        const bool isFile = body.value("type").toString() == "file";
        m_desc->setVisible(!m_compact || m_path.isEmpty());  // phone: room for code beats the description
        setBreadcrumb(m_path, isFile);
        m_viewer->setMarkedLines(0, 0, false);  // line marks belong to the file they were made in
        if (!isFile)
            m_pendingLines.clear();
        updateHash(nav);
        status(QStringLiteral("%1 @ %2").arg(ref(), body.value("commit").toString().left(12)));
        if (isFile)
            return showFile(body);
        m_outline->setVisible(false);
        m_viewToggle->setVisible(false);
        m_pendingSpan = {};
        m_pendingAnchor.clear();
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
        fitEntries();
        m_listScroll->verticalScrollBar()->setValue(0);
        loadReadme(body.value("entries").toArray());
        m_filesStack->setCurrentIndex(0);
    });
}

void ForgeWindow::fitEntries()
{
    const int rows = m_entries->topLevelItemCount();
    const int rowHeight = rows ? m_entries->sizeHintForRow(0) : 0;
    m_entries->setFixedHeight(m_entries->header()->sizeHint().height() + rows * rowHeight
                              + 2 * m_entries->frameWidth() + 2);
}

void ForgeWindow::loadReadme(const QJsonArray &entries)
{
    // The folder's README, like GitHub picks it: Markdown first, then other formats (shown as plain text).
    static const QStringList preferred = {"readme.md", "readme.markdown", "readme.mdown", "readme.mkd",
                                          "readme.rst", "readme.txt", "readme"};
    QString pick;
    int best = INT_MAX;
    for (const auto &v : entries) {
        const QString name = v.toObject().value("name").toString();
        if (name.endsWith('/'))
            continue;
        const QString lower = name.toLower();
        int rank = int(preferred.indexOf(lower));
        if (rank < 0 && lower.startsWith("readme."))
            rank = int(preferred.size());
        if (rank >= 0 && rank < best) {
            best = rank;
            pick = name;
        }
    }
    const int seq = ++m_readmeSeq;
    m_readmeBox->setVisible(false);  // until this folder's README has loaded
    if (pick.isEmpty())
        return;
    const QString repo = m_repo;
    const QString dir = m_path;
    const QString path = dir.isEmpty() ? pick : dir + "/" + pick;
    m_readmeTitle->setText(pick);
    if (MarkdownView::isMarkdown(pick)) {
        m_readme->showMarkdown(repo, ref(), path, QString(), [this, seq](bool ok) {
            if (seq == m_readmeSeq)
                m_readmeBox->setVisible(ok);
        });
        return;
    }
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("path", path);
    m_api->get(repoPath(repo) + "/tree", q, this, [this, seq, repo, dir, path](const QJsonObject &body,
                                                                             const QString &error) {
        if (seq != m_readmeSeq || repo != m_repo || dir != m_path)
            return;
        if (!error.isEmpty() || body.value("binary").toBool() || body.value("type").toString() != "file") {
            m_readmeBox->setVisible(false);
            return;
        }
        m_readmeBox->setVisible(true);
        m_readme->showPlain(repo, ref(), path, body.value("content").toString());
    });
}

void ForgeWindow::openRepoLink(const QString &path, const QString &fragment)
{
    // A link in a README: another file or folder at this ref; "#L12" opens it at that line, other
    // fragments are headings in a Markdown file.
    static const QRegularExpression lineRe(QStringLiteral("^L\\d+(-L?\\d+)?$"));
    if (lineRe.match(fragment).hasMatch())
        m_pendingLines = fragment;
    else
        m_pendingAnchor = fragment;
    closeNav();
    browse(path);
}

void ForgeWindow::setPreview(bool preview)
{
    m_previewBtn->setChecked(preview);
    m_codeBtn->setChecked(!preview);
    m_codeStack->setCurrentIndex(preview ? 1 : 0);
    if (preview && m_viewer->markedFirst() > 0) {
        m_viewer->setMarkedLines(0, 0, false);  // line links belong to the source view
        updateHash();
    }
}

void ForgeWindow::syncHighlightTheme()
{
    // Highlight colours follow the viewer: dark-on-light or light-on-dark (the browser's colour scheme).
    m_highlighter->setDark(m_viewer->palette().color(QPalette::Base).lightness() < 128);
}

void ForgeWindow::changeEvent(QEvent *event)
{
    QMainWindow::changeEvent(event);
    if (event->type() == QEvent::PaletteChange)
        syncHighlightTheme();
}

void ForgeWindow::showFile(const QJsonObject &file)
{
    QString lang;
    m_viewer->setNavEnabled(false);
    m_hoverCache.clear();
    m_outline->setVisible(false);
    ++m_hoverSeq;
    QToolTip::hideText();
    if (file.value("binary").toBool()) {
        m_highlighter->setFileName(QString());
        m_viewer->setPlainText(QStringLiteral("(binary file, %1)").arg(humanSize(file.value("size").toInteger())));
    } else {
        const QString content = file.value("content").toString();
        // Huge files stay plain: highlighting runs on the UI thread and would freeze the page.
        constexpr qsizetype HighlightLimit = 1 << 20;
        m_highlighter->setFileName(QString());
        m_viewer->setPlainText(content);
        if (content.size() <= HighlightLimit)
            lang = m_highlighter->setFileName(file.value("path").toString());
    }
    if (!lang.isEmpty())
        m_status->setText(m_status->text() + " · " + lang);
    // Markdown opens rendered, unless the link names lines (those live in the source).
    const bool markdown = !file.value("binary").toBool() && MarkdownView::isMarkdown(file.value("path").toString());
    m_viewToggle->setVisible(markdown);
    const QString anchor = std::exchange(m_pendingAnchor, QString());
    if (markdown)
        m_mdView->showMarkdown(m_repo, ref(), file.value("path").toString(), anchor);
    setPreview(markdown && m_pendingLines.isEmpty());
    m_filesStack->setCurrentIndex(1);
    // Line link from the URL (#L12 or #L12-L20): mark and scroll to it once the view has its size.
    const QString lines = std::exchange(m_pendingLines, QString());
    m_viewer->setMarkedLines(0, 0, false);
    if (!lines.isEmpty()) {
        const QStringList ends = lines.split('-');
        const int first = ends.value(0).mid(1).toInt();
        const QString lastStr = ends.value(1);
        const int last = lastStr.isEmpty() ? first : lastStr.mid(lastStr.startsWith('L') ? 1 : 0).toInt();
        const QJsonObject span = std::exchange(m_pendingSpan, QJsonObject());
        QTimer::singleShot(0, this, [this, first, last, span] {
            m_viewer->setMarkedLines(first, last, true);
            if (span.value("line").toInt() == first) {  // arrived from a definition/reference: select the name
                const QString text = m_viewer->document()->findBlockByNumber(first - 1).text();
                m_viewer->revealSpan(first, toUtf16(text, span.value("col").toInt()),
                                     toUtf16(text, span.value("end_col").toInt()));
            }
            updateHash();
        });
    }
    m_pendingSpan = {};
    m_filesStack->setCurrentIndex(1);
    if (!file.value("binary").toBool())
        loadOutline();
}

QUrlQuery ForgeWindow::symbolQuery(int line, int col) const
{
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("path", m_path);
    q.addQueryItem("line", QString::number(line));
    const QString text = m_viewer->document()->findBlockByNumber(line - 1).text();
    q.addQueryItem("col", QString::number(toCodePoints(text, col)));
    return q;
}

void ForgeWindow::loadOutline()
{
    const QString repo = m_repo, path = m_path;
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("path", path);
    m_api->get(repoPath(repo) + "/symbols", q, this, [this, repo, path](const QJsonObject &body, const QString &error) {
        if (repo != m_repo || path != m_path || m_filesStack->currentIndex() != 1)
            return;
        if (!error.isEmpty() || !body.value("supported").toBool())
            return;  // no code navigation for this file type: the viewer stays a plain viewer
        m_viewer->setNavEnabled(true);
        m_viewer->setToolTip(QString());
        const QJsonArray syms = body.value("symbols").toArray();
        const QSignalBlocker block(m_outline);
        m_outline->clear();
        m_outline->addItem(syms.isEmpty() ? QStringLiteral("No symbols") : QStringLiteral("Symbols (%1)").arg(syms.size()));
        for (const auto &v : syms) {
            const QJsonObject d = v.toObject();
            const QString container = d.value("container").toString();
            const QString label = (container.isEmpty() ? QString() : QStringLiteral("    "))
                + d.value("kind").toString() + " " + d.value("name").toString();
            m_outline->addItem(label, d);
            m_outline->setItemData(m_outline->count() - 1, d.value("signature").toString(), Qt::ToolTipRole);
        }
        m_outline->setVisible(true);
        status(m_status->text() + (isTouchUi() ? " · tap a name for its definition and references"
                                               : " · click a name to see its definition and references (Ctrl+click jumps)"));
    });
}

void ForgeWindow::lookupSymbol(int line, int col, bool jump)
{
    const QString repo = m_repo, path = m_path;
    const int seq = ++m_navSeq;
    const bool panelWasHidden = !m_navPanel->isVisible();
    QToolTip::hideText();
    const QString text = m_viewer->document()->findBlockByNumber(line - 1).text();
    int s = col, e = col;
    while (s > 0 && (text[s - 1].isLetterOrNumber() || text[s - 1] == '_' || text[s - 1] == '$'))
        --s;
    while (e < text.size() && (text[e].isLetterOrNumber() || text[e] == '_' || text[e] == '$'))
        ++e;
    if (!jump) {
        m_navPanel->setVisible(true);
        m_navPanel->setLoading(text.mid(s, e - s));
    }
    status("Looking up " + text.mid(s, e - s) + "…");
    m_api->get(repoPath(repo) + "/symbol", symbolQuery(line, col), this,
               [this, repo, path, seq, jump, panelWasHidden](const QJsonObject &r, const QString &error) {
        if (repo != m_repo || path != m_path || seq != m_navSeq)
            return;
        if (!error.isEmpty()) {
            m_navPanel->setError(error);
            return status("Lookup failed: " + error, true);
        }
        if (r.value("symbol").isNull()) {
            if (!jump)
                closeNav();
            return status("Nothing to look up there.");
        }
        const QString name = r.value("symbol").toObject().value("name").toString();
        const QJsonArray defs = r.value("definitions").toArray();
        const QJsonArray refs = r.value("references").toArray();
        // Underline the symbol everywhere it occurs in this file.
        QList<CodeView::Span> spans;
        for (const auto &v : refs) {
            const QJsonObject o = v.toObject();
            if (o.value("path").toString() != path)
                continue;
            const int ln = o.value("line").toInt();
            const QString t = m_viewer->document()->findBlockByNumber(ln - 1).text();
            spans.append({ln, toUtf16(t, o.value("col").toInt()), toUtf16(t, o.value("end_col").toInt())});
        }
        m_viewer->setSymbolMarks(spans);
        const QJsonObject sym = r.value("symbol").toObject();
        const QJsonObject first = defs.isEmpty() ? QJsonObject() : defs.first().toObject();
        const bool onDefinition = !first.isEmpty() && first.value("path").toString() == path
            && first.value("line").toInt() == sym.value("line").toInt() && first.value("col").toInt() == sym.value("col").toInt();
        if (jump && !first.isEmpty() && !onDefinition) {
            status(QStringLiteral("%1 → %2:%3").arg(name, first.value("path").toString()).arg(first.value("line").toInt()));
            return openPlace(first.value("path").toString(), first.value("line").toInt(), first.value("col").toInt(),
                             first.value("end_col").toInt());
        }
        m_navPanel->setVisible(true);
        m_navPanel->setResult(r);
        if (m_fileSplit->orientation() == Qt::Vertical) {  // phone: the panel takes the lower part of the screen
            if (panelWasHidden || m_fileSplit->sizes().value(1) < 120)
                m_fileSplit->setSizes({m_fileSplit->height() / 3, m_fileSplit->height() * 2 / 3});
        } else if (m_fileSplit->sizes().value(1) < 120) {
            m_fileSplit->setSizes({std::max(300, m_fileSplit->width() * 2 / 3), std::max(240, m_fileSplit->width() / 3)});
        }
        status(QStringLiteral("%1: %2 definition(s), %3 reference(s)")
                   .arg(name).arg(defs.size()).arg(r.value("reference_count").toInt(refs.size())));
    });
}

void ForgeWindow::hoverSymbol(int line, int col, const QPoint &globalPos)
{
    const auto show = [this, globalPos](const QJsonObject &r) {
        const QJsonArray defs = r.value("definitions").toArray();
        if (defs.isEmpty())
            return;
        const QJsonObject d = defs.first().toObject();
        QString html = "<pre style='margin:0'>" + d.value("signature").toString().toHtmlEscaped() + "</pre>";
        const QString doc = d.value("doc").toString();
        if (!doc.isEmpty()) {
            QString shortDoc = doc.left(800);
            if (shortDoc.size() < doc.size())
                shortDoc += "…";
            html += "<p style='white-space:pre-wrap; margin-top:6px'>" + shortDoc.toHtmlEscaped() + "</p>";
        }
        QString where = QStringLiteral("%1:%2").arg(d.value("path").toString()).arg(d.value("line").toInt());
        if (defs.size() > 1)
            where += QStringLiteral(" · and %1 more").arg(defs.size() - 1);
        html += "<p style='margin-top:6px'><i>" + where.toHtmlEscaped() + "</i> &nbsp;·&nbsp; click for references</p>";
        QToolTip::showText(globalPos, html, m_viewer);
    };
    const QString text = m_viewer->document()->findBlockByNumber(line - 1).text();
    const int cp = toCodePoints(text, col);
    for (const QJsonObject &r : std::as_const(m_hoverCache)) {
        const QJsonObject s = r.value("symbol").toObject();
        if (s.value("line").toInt() == line && s.value("col").toInt() <= cp && cp < s.value("end_col").toInt())
            return show(r);
    }
    const QString repo = m_repo, path = m_path;
    const int seq = ++m_hoverSeq;
    QUrlQuery q = symbolQuery(line, col);
    q.addQueryItem("references", "false");
    m_api->get(repoPath(repo) + "/symbol", q, this, [this, repo, path, seq, show](const QJsonObject &r, const QString &error) {
        if (!error.isEmpty() || repo != m_repo || path != m_path || r.value("symbol").isNull())
            return;
        if (m_hoverCache.size() > 200)
            m_hoverCache.clear();
        m_hoverCache.append(r);
        if (seq == m_hoverSeq && m_viewer->underMouse())
            show(r);
    });
}

void ForgeWindow::openPlace(const QString &path, int line, int col, int endCol, bool remember)
{
    if (remember && !m_path.isEmpty() && m_filesStack->currentIndex() == 1) {
        m_backStack.append({m_path, m_viewer->textCursor().blockNumber() + 1});
        if (m_backStack.size() > 100)
            m_backStack.removeFirst();
        m_navPanel->setBackEnabled(true);
    }
    if (path == m_path && m_filesStack->currentIndex() == 1) {
        setPreview(false);
        m_viewer->setMarkedLines(line, line, false);
        const QString text = m_viewer->document()->findBlockByNumber(line - 1).text();
        if (endCol > col)
            m_viewer->revealSpan(line, toUtf16(text, col), toUtf16(text, endCol));
        else
            m_viewer->setMarkedLines(line, line, true);
        updateHash(remember ? Nav::Push : Nav::Replace);
        return;
    }
    m_pendingLines = QStringLiteral("L%1").arg(line);
    m_pendingSpan = endCol > col ? QJsonObject{{"line", line}, {"col", col}, {"end_col", endCol}} : QJsonObject();
    browse(path, remember ? Nav::Push : Nav::Replace);
}

void ForgeWindow::goBack()
{
    if (m_backStack.isEmpty())
        return;
    const Place p = m_backStack.takeLast();
    m_navPanel->setBackEnabled(!m_backStack.isEmpty());
    openPlace(p.path, p.line, 0, 0, false);
}

void ForgeWindow::closeNav()
{
    ++m_navSeq;
    m_navPanel->setVisible(false);
    m_viewer->setSymbolMarks({});
}

void ForgeWindow::loadLog()
{
    const QString repo = m_repo;
    QUrlQuery q;
    q.addQueryItem("ref", ref());
    q.addQueryItem("limit", "100");
    setTabBadge(1, QString());
    m_api->get(repoPath(repo) + "/commits", q, this, [this, repo](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        const QSignalBlocker block(m_commits);
        m_commits->clear();
        if (!error.isEmpty()) {
            qWarning("forge: loading commits for %s failed: %s", qPrintable(repo), qPrintable(error));
            auto *it = new QTreeWidgetItem(m_commits);
            it->setText(0, "Could not load commits: " + error + " (open this tab again to retry)");
            setTabBadge(1, "error");
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
        selectPendingCommit();
        setTabBadge(1, QString::number(commits.size()));
    });
}

QString ForgeWindow::currentHash() const
{
    if (m_main->currentIndex() == 3)
        return "settings";
    if (m_repo.isEmpty() || m_main->currentIndex() != 1)
        return {};
    const int tab = m_tabs->currentIndex();
    QString hash = m_repo;
    QString item;
    if (tab == 0)
        hash += m_path.isEmpty() ? "" : "/" + m_path;
    else
        hash += "/-/" + TabNames.value(tab);
    if (tab == 1) {
        if (m_commitStack->currentIndex() == 1)
            item = m_commitView->sha().left(12);
    } else if (tab == 2) {
        item = m_releases->pickedTag();
    } else if (tab == 4) {
        item = m_runShown;
    }
    if (!item.isEmpty())
        hash += "/" + item;
    if ((tab == 0 || tab == 1) && !m_info.isEmpty() && ref() != defaultRef())
        hash += "?ref=" + ref();
    if (tab == 0 && m_filesStack->currentIndex() == 1 && m_viewer->markedFirst() > 0) {
        hash += QStringLiteral("#L%1").arg(m_viewer->markedFirst());
        if (m_viewer->markedLast() > m_viewer->markedFirst())
            hash += QStringLiteral("-L%1").arg(m_viewer->markedLast());
    }
    return hash;
}

void ForgeWindow::updateHash(Nav nav)
{
    if (m_main->currentIndex() == 2)
        return;  // the error page keeps the link that failed, so Try again and sign-in can retry it
    m_api->setLocationHash(currentHash(), nav == Nav::Push);
}

QWidget *ForgeWindow::buildSettings()
{
    auto *page = new QWidget;
    auto *lay = new QVBoxLayout(page);
    lay->setContentsMargins(8, 8, 8, 8);

    auto *general = new QGroupBox("General");
    auto *form = new QFormLayout(general);
    m_settingsForm = form;
    // Repos always belong to their owner (the signed-in handle); only the name part can change.
    auto *nameRow = new QHBoxLayout;
    m_ownerPrefix = new QLabel;
    nameRow->addWidget(m_ownerPrefix);
    m_editName = new QLineEdit;
    m_editName->setPlaceholderText("repo-name");
    m_editName->setToolTip("Renaming changes the clone URL; the old URL keeps working as an alias.");
    nameRow->addWidget(m_editName, 1);
    form->addRow("Name:", nameRow);
    m_editDesc = new QLineEdit;
    m_editDesc->setPlaceholderText("A short description");
    form->addRow("Description:", m_editDesc);
    m_defaultBranch = new QComboBox;
    m_defaultBranch->setMinimumWidth(160);
    form->addRow("Default branch:", m_defaultBranch);
    m_public = new QCheckBox("Public");
    auto *visRow = new QVBoxLayout;
    visRow->setSpacing(0);
    visRow->addWidget(m_public);
    auto *visHint = new QLabel("Anyone can browse and clone it without signing in.");
    visHint->setWordWrap(true);
    visHint->setEnabled(false);  // the dimmer "hint" colour
    visRow->addWidget(visHint);
    form->addRow("Visibility:", visRow);
    m_save = new QPushButton("Save changes");
    m_save->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    auto *saveRow = new QHBoxLayout;
    saveRow->addStretch(1);
    saveRow->addWidget(m_save);
    form->addRow(saveRow);
    lay->addWidget(general);
    connect(m_save, &QPushButton::clicked, this, &ForgeWindow::saveSettings);
    connect(m_editName, &QLineEdit::returnPressed, this, &ForgeWindow::saveSettings);
    connect(m_editDesc, &QLineEdit::returnPressed, this, &ForgeWindow::saveSettings);

    // No modal dialogs: they need a nested event loop, which Qt for WebAssembly can't run.
    auto *danger = new QGroupBox("Delete repository");
    danger->setStyleSheet("QGroupBox { color: #c62828; }");
    auto *dl = new QVBoxLayout(danger);
    auto *warn = new QLabel("This deletes the repository and all of its history. It can't be undone. "
                            "Type the repository's name to confirm.");
    warn->setWordWrap(true);
    dl->addWidget(warn);
    auto *delRow = new QHBoxLayout;
    m_confirmDelete = new QLineEdit;
    delRow->addWidget(m_confirmDelete, 1);
    m_delete = new QPushButton("Delete");
    m_delete->setIcon(style()->standardIcon(QStyle::SP_TrashIcon));
    m_delete->setEnabled(false);
    delRow->addWidget(m_delete);
    dl->addLayout(delRow);

    // Secrets: write-only. The list shows names and dates; a value can be replaced but never read back.
    // The owner's user secrets show too (dimmed): this repo's jobs get them unless it has its own.
    m_repoSecrets = new SecretsPanel(m_api, "Secrets",
        "Encrypted on the server and never shown again. Deploy jobs in <code>.forge/jobs.toml</code> get each one "
        "as an environment variable (see the Deploys tab), along with your "
        "<a href=\"#settings\">user secrets</a>; a repo secret of the same name wins.");
    connect(m_repoSecrets, &SecretsPanel::statusText, this, &ForgeWindow::status);
    connect(m_repoSecrets->noteLabel(), &QLabel::linkActivated, this, [this] { showUserSettings(); });
    lay->addWidget(m_repoSecrets);

    lay->addWidget(danger);
    lay->addStretch(1);
    connect(m_confirmDelete, &QLineEdit::textChanged, this, [this](const QString &t) {
        m_delete->setEnabled(!m_repo.isEmpty() && t.trimmed() == m_repo.section('/', 1));
    });
    connect(m_delete, &QPushButton::clicked, this, &ForgeWindow::deleteRepo);
    page->setEnabled(false);
    return page;
}

void ForgeWindow::fillSettings(const QJsonObject &info)
{
    m_ownerPrefix->setText(info.value("project").toString() + " /");
    m_editName->setText(info.value("name").toString());
    m_editDesc->setText(info.value("description").toString());
    m_public->setChecked(info.value("public").toBool());
    m_defaultBranch->clear();
    const QString def = info.value("default").toString();
    const QStringList branches = info.value("branches").toObject().keys();
    for (const QString &b : branches)
        m_defaultBranch->addItem(b);
    if (!def.isEmpty() && !branches.contains(def))
        m_defaultBranch->addItem(def);  // empty repo: HEAD names the branch the first push will create
    m_defaultBranch->setCurrentText(def);
    m_defaultBranch->setEnabled(m_defaultBranch->count() > 1);
    m_confirmDelete->clear();
    m_confirmDelete->setPlaceholderText(info.value("name").toString());
    m_settings->setEnabled(true);
    m_runBranch->clear();
    for (const QString &b : branches)
        m_runBranch->addItem(b);
    if (!def.isEmpty() && branches.contains(def))
        m_runBranch->setCurrentText(def);
    m_runNow->setEnabled(!branches.isEmpty());
    if (m_signedIn) {
        m_repoSecrets->load();
        if (m_tabs->currentIndex() == 4)
            loadRuns();
    }
}

void ForgeWindow::saveSettings()
{
    if (m_repo.isEmpty() || m_info.isEmpty())
        return;
    QJsonObject body;
    const QString name = m_editName->text().trimmed();
    if (!name.isEmpty() && name != m_info.value("name").toString())
        body["name"] = name;
    if (m_editDesc->text().trimmed() != m_info.value("description").toString())
        body["description"] = m_editDesc->text().trimmed();
    if (m_public->isChecked() != m_info.value("public").toBool())
        body["public"] = m_public->isChecked();
    const QString branch = m_defaultBranch->currentText();
    if (!branch.isEmpty() && branch != m_info.value("default").toString())
        body["default_branch"] = branch;
    if (body.isEmpty())
        return status("Nothing to save.");
    const QString repo = m_repo;
    m_save->setEnabled(false);
    status("Saving settings…");
    m_api->send("PATCH", repoPath(repo), body, this, [this, repo](const QJsonObject &info, const QString &error) {
        m_save->setEnabled(true);
        if (!error.isEmpty())
            return status("Could not save settings: " + error, true);
        const QString now = info.value("repo").toString();
        m_pendingPath.clear();
        m_api->setLocationHash(now + "/-/settings");
        m_repo.clear();  // reopen (as the new name, if renamed) once the list reloads
        loadRepos();
        status("Settings saved.");
    });
}

void ForgeWindow::deleteRepo()
{
    const QString repo = m_repo;
    if (repo.isEmpty() || m_confirmDelete->text().trimmed() != repo.section('/', 1))
        return;
    m_delete->setEnabled(false);
    status("Deleting " + repo + "…");
    m_api->send("DELETE", repoPath(repo), {}, this, [this, repo](const QJsonObject &, const QString &error) {
        if (!error.isEmpty()) {
            m_delete->setEnabled(true);
            return status("Could not delete " + repo + ": " + error, true);
        }
        m_repo.clear();
        m_api->setLocationHash(QString(), true);
        m_main->setCurrentIndex(0);
        m_tabs->setCurrentIndex(0);
        loadRepos();
        status("Deleted " + repo + ".");
    });
}

void ForgeWindow::showUserSettings(Nav nav)
{
    m_repo.clear();
    m_pendingPath.clear();
    m_hasPendingTab = false;
    {
        const QSignalBlocker block(m_repos);
        m_repos->setCurrentRow(-1);  // so clicking any repo (even the one that was open) opens it again
    }
    m_api->setLocationHash("settings", nav == Nav::Push);
    m_main->setCurrentIndex(3);
    m_userSecrets->load();
    status("Your settings");
}

QWidget *ForgeWindow::buildDeploys()
{
    auto *page = new QWidget;
    auto *lay = new QVBoxLayout(page);
    lay->setContentsMargins(8, 8, 8, 8);
    m_runsNote = new QLabel(
        "Every push runs the jobs in <code>.forge/jobs.toml</code> whose <code>branches</code> match (default: the "
        "default branch). A job is a shell script (<code>run</code>) in a checkout of the commit, or an HTTP request "
        "(<code>post</code>, for deploy hooks). Repo and user secrets are environment variables, and "
        "<code>${NAME}</code> in a post's URL, headers or body; they are masked in the output.");
    m_runsNote->setWordWrap(true);
    m_runsNote->setTextFormat(Qt::RichText);
    lay->addWidget(m_runsNote);
    auto *row = new QHBoxLayout;
    m_runBranchLabel = new QLabel("Branch:");
    row->addWidget(m_runBranchLabel);
    m_runBranch = new QComboBox;
    m_runBranch->setMinimumWidth(140);
    row->addWidget(m_runBranch);
    m_runNow = new QPushButton("Run now");
    m_runNow->setIcon(style()->standardIcon(QStyle::SP_MediaPlay));
    m_runNow->setToolTip("Run the jobs for this branch's latest commit, as a push would");
    row->addWidget(m_runNow);
    row->addStretch(1);
    auto *refresh = new QPushButton("Refresh");
    m_runRefresh = refresh;
    refresh->setToolTip("Refresh");
    refresh->setIcon(style()->standardIcon(QStyle::SP_BrowserReload));
    row->addWidget(refresh);
    lay->addLayout(row);
    auto *split = new QSplitter(Qt::Vertical);
    m_runs = new QTreeWidget;
    m_runs->setHeaderLabels({"Status", "Job", "Branch", "Commit", "Started", "Time", "Trigger", "Depot"});
    m_runs->setRootIsDecorated(false);
    m_runs->setUniformRowHeights(true);
    m_runs->header()->setStretchLastSection(false);
    for (int i = 0; i < 8; ++i)
        m_runs->header()->setSectionResizeMode(i, i == 1 ? QHeaderView::Stretch : QHeaderView::ResizeToContents);
    touchScroll(m_runs);
    split->addWidget(m_runs);
    // The run's output, with a link to its Depot build (or Depot CI run) above it when it has one.
    auto *detail = new QWidget;
    auto *detailLay = new QVBoxLayout(detail);
    detailLay->setContentsMargins(0, 0, 0, 0);
    detailLay->setSpacing(4);
    m_runLink = new QLabel;
    m_runLink->setTextFormat(Qt::RichText);
    m_runLink->setTextInteractionFlags(Qt::TextBrowserInteraction);
    m_runLink->setOpenExternalLinks(true);
    m_runLink->setVisible(false);
    detailLay->addWidget(m_runLink);
    m_runOutput = new QPlainTextEdit;
    m_runOutput->setReadOnly(true);
    m_runOutput->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
    m_runOutput->setPlaceholderText("Select a run to see its output.");
    m_runOutput->setLineWrapMode(QPlainTextEdit::NoWrap);
    if (isTouchUi()) {
        m_runOutput->setTextInteractionFlags(Qt::TextSelectableByKeyboard);
        touchScroll(m_runOutput);
    }
    detailLay->addWidget(m_runOutput, 1);
    split->addWidget(detail);
    split->setStretchFactor(0, 1);
    split->setStretchFactor(1, 2);
    lay->addWidget(split, 1);
    connect(m_runNow, &QPushButton::clicked, this, &ForgeWindow::startRun);
    connect(refresh, &QPushButton::clicked, this, &ForgeWindow::loadRuns);
    // Clicking a run's Depot cell opens the build on depot.dev.
    connect(m_runs, &QTreeWidget::itemClicked, this, [](QTreeWidgetItem *it, int column) {
        const QString url = it->data(7, PathRole).toString();
        if (column == 7 && !url.isEmpty())
            QDesktopServices::openUrl(QUrl(url));
    });
    connect(m_runs, &QTreeWidget::itemSelectionChanged, this, [this] {
        const auto sel = m_runs->selectedItems();
        if (!sel.isEmpty() && !sel.first()->data(0, PathRole).toString().isEmpty()) {
            showRun(sel.first()->data(0, PathRole).toString());
            updateHash(Nav::Push);  // #<repo>/-/deploys/<run>
        }
    });
    // While a run is queued or going, poll so its status and output appear without clicking Refresh.
    m_runsTimer = new QTimer(this);
    m_runsTimer->setInterval(2500);
    m_runsTimer->setSingleShot(true);
    connect(m_runsTimer, &QTimer::timeout, this, [this] {
        if (m_tabs->currentIndex() == 4 && !m_repo.isEmpty())
            loadRuns();
    });
    return page;
}

void ForgeWindow::loadRuns()
{
    const QString repo = m_repo;
    if (repo.isEmpty() || !m_signedIn)
        return;
    m_api->get(repoPath(repo) + "/runs", {}, this, [this, repo](const QJsonObject &body, const QString &error) {
        if (repo != m_repo)
            return;
        const QString keep = m_runShown;
        const QSignalBlocker block(m_runs);
        m_runs->clear();
        if (!error.isEmpty()) {
            (new QTreeWidgetItem(m_runs))->setText(1, "Couldn't load runs: " + error);
            return;
        }
        if (!body.value("enabled").toBool(true)) {
            (new QTreeWidgetItem(m_runs))->setText(1, "Deploy jobs are turned off on this server (FILEAPI_JOBS)");
            m_runNow->setEnabled(false);
            return;
        }
        const QJsonArray runs = body.value("runs").toArray();
        if (runs.isEmpty())
            (new QTreeWidgetItem(m_runs))->setText(1, "No runs yet. Commit " + body.value("jobs_file").toString()
                                                         + " and push.");
        bool active = false;
        for (const auto &v : runs) {
            const QJsonObject r = v.toObject();
            const QString st = r.value("status").toString();
            active = active || st == "queued" || st == "running";
            auto *it = new QTreeWidgetItem(m_runs);
            it->setText(0, st);
            QStyle::StandardPixmap icon = QStyle::SP_MessageBoxInformation;
            if (st == "success")
                icon = QStyle::SP_DialogApplyButton;
            else if (st == "failed" || st == "error" || st == "timeout" || st == "lost")
                icon = QStyle::SP_MessageBoxCritical;
            else if (st == "running" || st == "queued")
                icon = QStyle::SP_BrowserReload;
            it->setIcon(0, style()->standardIcon(icon));
            it->setText(1, r.value("job").toString());
            it->setText(2, r.value("tag").toString().isEmpty() ? r.value("branch").toString()
                                                               : "tag " + r.value("tag").toString());
            it->setText(3, r.value("sha").toString().left(10));
            it->setFont(3, QFontDatabase::systemFont(QFontDatabase::FixedFont));
            const QString started = r.value("started").toString(r.value("queued").toString());
            it->setText(4, QLocale().toString(QDateTime::fromString(started, Qt::ISODate).toLocalTime(),
                                              QLocale::ShortFormat));
            if (r.contains("duration"))
                it->setText(5, QString::number(r.value("duration").toDouble(), 'f', 1) + " s");
            it->setText(6, r.value("trigger").toString());
            const QString depotUrl = r.value("depot").toObject().value("url").toString();
            if (!depotUrl.isEmpty()) {
                it->setText(7, "Open");
                it->setData(7, PathRole, depotUrl);
                it->setForeground(7, palette().brush(QPalette::Link));
                it->setToolTip(7, depotUrl);
            }
            it->setData(0, PathRole, r.value("id").toString());
            if (r.value("id").toString() == keep)
                it->setSelected(true);
        }
        setTabBadge(4, active ? "running" : QString());
        if (!keep.isEmpty())
            showRun(keep);  // refresh the output of the run being watched
        if (active)
            m_runsTimer->start();
    });
}

void ForgeWindow::showRun(const QString &id)
{
    const QString repo = m_repo;
    m_runShown = id;
    m_api->get(repoPath(repo) + "/runs/" + id, {}, this, [this, repo, id](const QJsonObject &r, const QString &error) {
        if (repo != m_repo || id != m_runShown)
            return;
        if (!error.isEmpty()) {
            m_runLink->setVisible(false);
            return m_runOutput->setPlainText("Couldn't load this run: " + error);
        }
        const QJsonObject depot = r.value("depot").toObject();
        const QString depotUrl = depot.value("url").toString();
        if (depotUrl.isEmpty()) {
            m_runLink->setVisible(false);
        } else {
            const QString what = depot.contains("ci_run_id") ? "Depot CI run" : "Depot build";
            m_runLink->setText(QStringLiteral("<a href=\"%1\">Open the %2 on depot.dev</a>")
                                   .arg(depotUrl.toHtmlEscaped(), what));
            m_runLink->setToolTip(depotUrl);
            m_runLink->setVisible(true);
        }
        QString text = QStringLiteral("%1 · %2 · %3@%4\n").arg(r.value("job").toString(), r.value("status").toString(),
                                                                r.value("branch").toString(),
                                                                r.value("sha").toString().left(12));
        if (r.contains("exit_code") && !r.value("exit_code").isNull())
            text += QStringLiteral("exit code %1\n").arg(r.value("exit_code").toInt());
        if (r.contains("error"))
            text += "error: " + r.value("error").toString() + "\n";
        text += "\n" + r.value("output").toString();
        auto *bar = m_runOutput->verticalScrollBar();
        const bool atEnd = bar->value() >= bar->maximum() - 4;
        m_runOutput->setPlainText(text);
        if (atEnd)
            bar->setValue(bar->maximum());  // follow a running job's output
    });
}

void ForgeWindow::startRun()
{
    const QString repo = m_repo;
    const QString branch = m_runBranch->currentText();
    if (repo.isEmpty() || branch.isEmpty())
        return;
    QJsonObject body;
    body["ref"] = branch;
    m_runNow->setEnabled(false);
    status("Starting deploy jobs for " + branch + "…");
    m_api->send("POST", repoPath(repo) + "/runs", body, this, [this, branch](const QJsonObject &res, const QString &error) {
        m_runNow->setEnabled(true);
        if (!error.isEmpty())
            return status("Could not start: " + error, true);
        const QJsonArray runs = res.value("runs").toArray();
        status(QStringLiteral("Started %1 job(s) for %2.").arg(runs.size()).arg(branch));
        if (!runs.isEmpty())
            m_runShown = runs.first().toObject().value("id").toString();
        updateHash(Nav::Replace);
        loadRuns();
    });
}

QString ForgeWindow::shortName(const QString &repo) const
{
    // Your own repos show by name alone; anyone else's keep their owner.
    return !m_owner.isEmpty() && repo.section('/', 0, 0) == m_owner ? repo.section('/', 1) : repo;
}

void ForgeWindow::createRepo()
{
    const QString name = m_newName->text().trimmed();
    if (name.isEmpty())
        return status("Type a name for the new repository.");
    QJsonObject body;
    body["name"] = name;
    m_create->setEnabled(false);
    status("Creating " + name + "…");
    m_api->send("POST", "repos", body, this, [this, name](const QJsonObject &info, const QString &error) {
        m_create->setEnabled(true);
        if (!error.isEmpty())
            return status("Could not create " + name + ": " + error, true);
        m_newName->clear();
        const QString repo = info.value("repo").toString();
        m_pendingPath.clear();
        m_api->setLocationHash(repo, true);
        m_repo.clear();
        loadRepos();  // selects it from the hash
        status("Created " + repo + ". Push to " + info.value("clone_url").toString());
    });
}
