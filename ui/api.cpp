#include "api.h"

#include <QJsonDocument>
#include <QNetworkReply>
#include <QProcessEnvironment>

#ifdef Q_OS_WASM
#include <emscripten/val.h>
using emscripten::val;
#endif

Api::Api(QObject *parent) : QObject(parent)
{
#ifdef Q_OS_WASM
    m_base = QString::fromStdString(val::global("location")["origin"].as<std::string>());
#else
    m_base = QProcessEnvironment::systemEnvironment().value("FILEAPI_URL", "http://127.0.0.1:8000");
#endif
    while (m_base.endsWith('/'))
        m_base.chop(1);
}

QString Api::token() const
{
#ifdef Q_OS_WASM
    val fn = val::global("fileapiToken");
    if (fn.typeOf().as<std::string>() == "function") {
        val t = fn();
        if (t.typeOf().as<std::string>() == "string")
            return QString::fromStdString(t.as<std::string>());
    }
    return {};
#else
    return QProcessEnvironment::systemEnvironment().value("FILEAPI_TOKEN");
#endif
}

QNetworkRequest Api::request(const QString &path, const QUrlQuery &query) const
{
    QUrl url(m_base + "/api/forge/" + path);
    url.setQuery(query);
    QNetworkRequest req(url);
    req.setRawHeader("Accept", "application/json");
    const QString t = token();
    if (!t.isEmpty())
        req.setRawHeader("Authorization", "Bearer " + t.toUtf8());
    return req;
}

void Api::get(const QString &path, const QUrlQuery &query, QObject *context, Callback done)
{
    finish(m_net.get(request(path, query)), context, done);
}

void Api::send(const QByteArray &method, const QString &path, const QJsonObject &body, QObject *context, Callback done)
{
    QNetworkRequest req = request(path);
    QByteArray data;
    if (!body.isEmpty()) {
        req.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
        data = QJsonDocument(body).toJson(QJsonDocument::Compact);
    }
    finish(m_net.sendCustomRequest(req, method, data), context, done);
}

void Api::finish(QNetworkReply *reply, QObject *context, Callback done)
{
    connect(reply, &QNetworkReply::finished, context, [reply, done]() {
        reply->deleteLater();
        const QByteArray data = reply->readAll();
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const QJsonObject body = QJsonDocument::fromJson(data).object();
        QString error;
        if (status >= 400 || (status == 0 && reply->error() != QNetworkReply::NoError)) {
            error = body.value("detail").toString();
            if (error.isEmpty())
                error = status ? QStringLiteral("HTTP %1").arg(status) : reply->errorString();
        }
        done(body, error);
    });
}

void Api::signIn()
{
#ifdef Q_OS_WASM
    val::global("fileapiSignIn")();
#endif
}

void Api::signOut()
{
#ifdef Q_OS_WASM
    val::global("fileapiSignOut")();
#endif
}

QString Api::locationHash() const
{
#ifdef Q_OS_WASM
    QString h = QString::fromStdString(val::global("location")["hash"].as<std::string>());
    return QUrl::fromPercentEncoding(h.mid(h.startsWith('#') ? 1 : 0).toUtf8());
#else
    return {};
#endif
}

void Api::setLocationHash(const QString &hash)
{
#ifdef Q_OS_WASM
    val history = val::global("history");
    history.call<void>("replaceState", val::null(), std::string(""), std::string(("#" + hash).toStdString()));
#else
    Q_UNUSED(hash);
#endif
}
