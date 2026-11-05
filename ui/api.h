// Small client for fileapi's forge JSON API (/api/forge/...).
#pragma once

#include <QJsonObject>
#include <QNetworkAccessManager>
#include <QNetworkRequest>
#include <QObject>
#include <QUrlQuery>
#include <functional>

class Api : public QObject {
    Q_OBJECT
public:
    using Callback = std::function<void(const QJsonObject &body, const QString &error)>;

    explicit Api(QObject *parent = nullptr);

    QString baseUrl() const { return m_base; }
    bool hasToken() const { return !token().isEmpty(); }
    QString token() const;

    // GET <base>/api/forge/<path>?<query>; `error` is empty on success.
    void get(const QString &path, const QUrlQuery &query, QObject *context, Callback done);
    // PATCH/DELETE/... <base>/api/forge/<path> with an optional JSON body.
    void send(const QByteArray &method, const QString &path, const QJsonObject &body, QObject *context, Callback done);

    // Hand-offs to the page's JavaScript (index.html), which runs the OAuth sign-in.
    void signIn();
    void signOut();

    // The #<project>/<repo> part of the page URL, so links to a repo can be shared.
    QString locationHash() const;
    void setLocationHash(const QString &hash);

private:
    QNetworkRequest request(const QString &path, const QUrlQuery &query = {}) const;
    void finish(QNetworkReply *reply, QObject *context, Callback done);

    QNetworkAccessManager m_net;
    QString m_base;
};
