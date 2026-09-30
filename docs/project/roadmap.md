# Phase 6: Advanced Features Roadmap

## Overview

Phase 6 represents the advanced feature set that elevates Waddles from a solid multi-platform chatbot into a comprehensive community management platform with AI-powered moderation, modern API capabilities, cross-platform calendar sync, and sophisticated engagement systems.

**Timeline**: 8-12 weeks (after Phases 1-5)
**Complexity**: High
**Priority**: Medium-High (strategic features for competitive differentiation)

---

## 6.1 Core Modules Completion

### Identity Module Enhancements

**Status**: Foundation exists, needs advanced features
**File**: `core/identity_core_module/`
**Estimated Effort**: 2-3 weeks

#### Features to Implement

##### 1. Unified Identity Dashboard
- **User Profile Aggregation**: Single view across all linked platforms
- **Cross-Platform Activity Feed**: Unified timeline of user activity (messages, subscriptions, donations)
- **Identity Merge System**: Resolve duplicate identities with approval workflow
- **Privacy Controls**: Per-platform visibility settings, data export (GDPR)

##### 2. Advanced Verification Methods
- **OAuth-Based Verification**: Direct OAuth linking for Discord, Twitch, Slack (eliminate whisper requirement)
- **Email Verification**: Backup verification via email codes
- **Two-Factor Authentication (2FA)**: TOTP support for hub accounts
- **Verification History**: Audit trail of all verification attempts and methods

##### 3. Identity Reputation Score
- **Cross-Platform Reputation**: Aggregate reputation scores from all linked platforms
- **Trust Badges**: Visual indicators for verified, active, trusted users
- **Account Age Weighting**: Older accounts with higher trust scores
- **Suspicious Pattern Detection**: Flag new accounts linking to known bad actors

#### Technical Implementation

```python
# New services to create:
identity_core_module/
  services/
    unified_profile_service.py       # Aggregate profile data
    oauth_verification_service.py    # Direct OAuth linking
    identity_merge_service.py        # Merge duplicate identities
    reputation_aggregator.py         # Cross-platform reputation
    privacy_service.py               # Privacy controls

# Database tables:
identity_unified_profiles         # Aggregated user profiles
identity_verification_history     # Verification audit trail
identity_merge_requests           # Identity merge workflows
identity_privacy_settings         # Per-user privacy controls
identity_2fa_secrets              # TOTP secrets (encrypted)
```

#### Dependencies
- **OAuth Providers**: Requires OAuth apps for Discord, Twitch, Slack
- **Reputation Module**: Integration with `core/reputation_module/`
- **Email Service**: Hub module SMTP service
- **Encryption**: For storing 2FA secrets (use `cryptography` library)

---

### Community Module Features

**Status**: Basic structure exists, needs advanced community management
**File**: `core/community_module/`
**Estimated Effort**: 2-3 weeks

#### Features to Implement

##### 1. Community Health Dashboard
- **Activity Metrics**: Daily/weekly active users, message volume, engagement trends
- **Retention Analytics**: User retention curves, churn prediction
- **Growth Metrics**: New member tracking, activation funnels
- **Moderation Metrics**: Warnings issued, bans, content filter hits
- **Platform Breakdown**: Activity per platform (Discord vs Twitch vs Slack)

##### 2. Community Segmentation
- **User Cohorts**: Group users by join date, activity level, platform
- **Targeted Announcements**: Send announcements to specific user segments
- **A/B Testing Framework**: Test different bot behaviors with user groups
- **Custom Roles & Tiers**: Define custom user tiers beyond platform roles

##### 3. Cross-Platform Events
- **Synchronized Broadcasts**: Announce events across all platforms simultaneously
- **Platform-Specific Formatting**: Adapt messages to Discord embeds, Twitch chat, Slack blocks
- **Event Reminders**: Multi-platform reminder system (1 hour, 15 min, starting now)
- **Calendar Integration**: Link to Phase 6.4 advanced calendar features

##### 4. Community Templates
- **Onboarding Templates**: Pre-configured welcome messages, rules, role assignments
- **Configuration Presets**: Gaming community, podcast, education, business
- **Import/Export**: Share community configurations between Waddles instances
- **Best Practices Library**: Curated templates from successful communities

#### Technical Implementation

```python
# New services to create:
community_module/
  services/
    health_dashboard_service.py      # Community health metrics
    segmentation_service.py          # User cohort management
    broadcast_service.py             # Multi-platform announcements
    template_service.py              # Configuration templates
    ab_testing_service.py            # A/B test framework

# Database tables:
community_health_metrics          # Computed health scores
community_user_segments           # User cohort definitions
community_broadcasts              # Multi-platform announcements
community_templates               # Configuration templates
community_ab_tests                # A/B test configurations
```

#### Dependencies
- **Analytics Module**: `core/analytics_core_module/` for metrics
- **All Trigger Modules**: For multi-platform broadcasting
- **Browser Source**: For dashboard display in OBS

---

### Implementation Priorities

**Priority 1 (Must Have)**:
1. Identity OAuth verification (eliminate whisper dependency)
2. Community health dashboard (visibility into community state)
3. Cross-platform broadcast system (unified announcements)

**Priority 2 (Should Have)**:
4. Unified identity dashboard (better user experience)
5. Community segmentation (targeted communication)
6. Configuration templates (faster onboarding)

**Priority 3 (Nice to Have)**:
7. Identity merge system (edge case handling)
8. A/B testing framework (advanced optimization)
9. 2FA support (security enhancement)

---

## 6.2 ML-Based Bot Detection

### Overview

Evolve the current rule-based bot detection (`core/analytics_core_module/services/bot_score_service.py`) into a machine learning-powered system that learns from community patterns and continuously improves detection accuracy.

**Estimated Effort**: 4-5 weeks
**Complexity**: High (requires ML expertise)

### Current State Analysis

**Existing System** (Rule-Based):
- Weighted formula combining 4 factors: bad_actor (30%), reputation (25%), security (20%), ai_behavioral (25%)
- Pattern detection: rapid posting, duplicate messages, unusual timing
- Scoring: 0-100 with A-F grades
- Database: `analytics_bot_scores`, `analytics_suspected_bots`

**Limitations**:
- Static thresholds don't adapt to community norms
- Can't detect novel bot patterns
- High false positive rate in active communities
- No learning from moderator feedback

### ML Model Architecture

#### Phase 1: Feature Engineering (Week 1-2)

**Features to Extract** (per user per community):

**Behavioral Features**:
- Message frequency patterns (mean, std dev, peaks)
- Inter-message timing distribution
- Message length statistics
- Vocabulary diversity (unique words per message)
- Response latency to others' messages
- Active hours distribution

**Content Features**:
- Repetition rate (identical messages)
- URL/link density
- Emoji/emoticon usage
- Capitalization patterns
- Special character frequency
- Language consistency (gibberish detection)

**Social Features**:
- Reply/mention patterns
- Network centrality (who they interact with)
- Conversation initiation rate
- Thread participation depth
- @mention frequency

**Account Features**:
- Account age (days since first message)
- Platform history (age of Twitch/Discord account)
- Cross-platform presence (linked accounts)
- Reputation score
- Moderation history

**Community Context Features**:
- Community size category
- Average message rate (normalization)
- Typical vocabulary (deviation score)

```python
# Feature extraction service:
analytics_core_module/
  ml/
    feature_extractor.py           # Extract features from message history
    feature_engineering.py         # Transform and normalize features
    feature_store.py               # Cache computed features
```

#### Phase 2: Model Training Pipeline (Week 2-3)

**Model Selection**:
- **Primary Model**: Gradient Boosted Trees (XGBoost or LightGBM)
  - Handles tabular data well
  - Interpretable feature importance
  - Fast inference
  - Works with imbalanced data

- **Alternative**: Random Forest
  - Good baseline, less overfitting
  - Feature importance available

- **Advanced**: Neural Network (if dataset is large enough)
  - Better at capturing complex patterns
  - Requires more data (1000+ labeled examples)

**Training Data Sources**:
1. **Existing Labels**: Suspected bots marked by moderators (is_false_positive field)
2. **Heuristic Labels**: High-confidence rule-based detections
3. **Import Known Bot Lists**: Public databases of spam accounts
4. **Active Learning**: Flag uncertain cases for moderator review

```python
# Training pipeline:
analytics_core_module/
  ml/
    model_trainer.py               # Train ML models
    hyperparameter_tuner.py        # Optimize model parameters
    model_validator.py             # Cross-validation, metrics
    model_registry.py              # Version and store models
```

**Training Process**:
1. Extract features for all users (past 30 days)
2. Label users: confirmed bots (1), confirmed humans (0), uncertain (skip)
3. Split: 70% train, 15% validation, 15% test
4. Handle class imbalance: SMOTE, class weights, or focal loss
5. Train model with cross-validation
6. Evaluate: Precision, Recall, F1, AUC-ROC
7. Feature importance analysis
8. Hyperparameter tuning
9. Final model selection

**Success Metrics**:
- **Precision > 85%**: Low false positives (don't ban humans)
- **Recall > 75%**: Catch most bots
- **F1 > 80%**: Balanced performance
- **AUC-ROC > 0.90**: Strong discrimination

#### Phase 3: Model Deployment (Week 3-4)

**Inference Architecture**:

```python
# Inference system:
analytics_core_module/
  ml/
    inference_engine.py            # Load model and predict
    batch_scorer.py                # Score all community users
    realtime_scorer.py             # Score new users immediately
    threshold_optimizer.py         # Adjust thresholds per community
```

**Deployment Strategy**:
- **Model Storage**: SeaweedFS (S3-compatible) for model artifacts
- **Model Format**: ONNX for cross-platform inference
- **Inference Service**: FastAPI endpoint for real-time scoring
- **Batch Jobs**: Nightly recalculation for all users
- **Model Updates**: Monthly retraining with new data

**Scoring Process**:
1. **New User Detection**: Trigger scoring after 10-50 messages
2. **Feature Extraction**: Compute features from user's message history
3. **Model Inference**: Get bot probability (0.0-1.0)
4. **Threshold Application**: Per-community thresholds (default: 0.7)
5. **Confidence Score**: Map probability to 0-100 scale
6. **Alert Generation**: Create `analytics_suspected_bots` record if threshold exceeded

#### Phase 4: Continuous Learning (Week 4-5)

**Feedback Loop**:
1. **Moderator Feedback**: Capture all `mark_bot_reviewed()` calls
2. **Feedback Storage**: Store feedback in training dataset
3. **Automated Retraining**: Weekly retraining with new feedback
4. **Model A/B Testing**: Deploy new model to 10% of communities, compare metrics
5. **Gradual Rollout**: Promote to 50%, then 100% if performance improves

**Active Learning**:
- **Uncertainty Sampling**: Flag users where model confidence is low (0.4-0.6)
- **Moderator Review Queue**: Present uncertain cases for manual review
- **Label Efficiency**: Prioritize labeling samples that improve model most

```python
# Continuous learning:
analytics_core_module/
  ml/
    feedback_collector.py          # Collect moderator feedback
    retraining_job.py              # Automated retraining pipeline
    ab_testing_manager.py          # Deploy and evaluate new models
    active_learner.py              # Select samples for labeling
```

### A/B Testing Approach

**Experiment Design**:
- **Control Group**: Current rule-based system
- **Treatment Group**: ML-based system
- **Split**: 50/50 by community_id hash
- **Duration**: 2-4 weeks
- **Metrics to Track**:
  - Detection rate (suspected bots flagged)
  - False positive rate (humans incorrectly flagged)
  - Moderator review time (time to review queue)
  - Moderator satisfaction (survey)

**Success Criteria**:
- ML system reduces false positives by >20%
- ML system maintains or improves detection rate
- Moderator satisfaction increases

### Database Changes

```sql
-- New tables:
CREATE TABLE analytics_ml_features (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    hub_user_id INTEGER NOT NULL,
    features JSONB NOT NULL,                -- Extracted feature vector
    computed_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(community_id, hub_user_id)
);
CREATE INDEX idx_ml_features_lookup ON analytics_ml_features(community_id, hub_user_id);

CREATE TABLE analytics_ml_models (
    id SERIAL PRIMARY KEY,
    model_name VARCHAR(255) NOT NULL,
    version VARCHAR(50) NOT NULL,
    model_artifact_url TEXT NOT NULL,      -- SeaweedFS URL
    metrics JSONB,                          -- Precision, recall, F1, AUC
    training_date TIMESTAMP DEFAULT NOW(),
    deployed_at TIMESTAMP,
    status VARCHAR(50) DEFAULT 'training',  -- training, testing, deployed, retired
    UNIQUE(model_name, version)
);

CREATE TABLE analytics_ml_predictions (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    hub_user_id INTEGER NOT NULL,
    model_id INTEGER REFERENCES analytics_ml_models(id),
    bot_probability FLOAT NOT NULL,         -- 0.0-1.0
    confidence_score INTEGER NOT NULL,      -- 0-100
    features_used JSONB,                    -- Feature values used for prediction
    predicted_at TIMESTAMP DEFAULT NOW(),
    INDEX idx_ml_predictions_lookup (community_id, hub_user_id, predicted_at DESC)
);

CREATE TABLE analytics_ml_feedback (
    id SERIAL PRIMARY KEY,
    prediction_id INTEGER REFERENCES analytics_ml_predictions(id),
    community_id INTEGER NOT NULL,
    hub_user_id INTEGER NOT NULL,
    is_bot BOOLEAN NOT NULL,                -- Ground truth label
    reviewer_id INTEGER NOT NULL,
    feedback_date TIMESTAMP DEFAULT NOW()
);
```

### Infrastructure Requirements

**Compute**:
- Training: GPU recommended (1x NVIDIA T4 or better) for neural networks
- Inference: CPU sufficient (model optimized with ONNX)
- Batch scoring: 10-20 workers for parallel feature extraction

**Storage**:
- SeaweedFS bucket for model artifacts (1-10 GB per model)
- PostgreSQL for features, predictions, feedback (100 MB - 10 GB)

**Libraries**:
```txt
scikit-learn==1.5.0
xgboost==2.0.3
lightgbm==4.3.0
onnx==1.16.0
onnxruntime==1.17.0
imbalanced-learn==0.12.0         # SMOTE for class imbalance
shap==0.44.0                     # Model interpretability
mlflow==2.11.0                   # Experiment tracking (optional)
```

### Rollout Plan

**Week 1-2**: Feature engineering and extraction
**Week 3**: Initial model training and validation
**Week 4**: Deployment infrastructure setup (inference service)
**Week 5**: A/B testing with 10% of communities
**Week 6-7**: Feedback collection and model iteration
**Week 8**: Full rollout to 100% of communities

### Monitoring and Maintenance

**Metrics Dashboard**:
- Model performance over time (precision, recall, F1)
- Feature importance drift
- Prediction distribution (bot probability histogram)
- Feedback rate (% of predictions reviewed)
- False positive/negative rates

**Alerts**:
- Model performance degradation (F1 drops >5%)
- Feature extraction failures
- Inference latency spikes (>500ms p99)
- Training job failures

**Maintenance Schedule**:
- Weekly automated retraining with new feedback
- Monthly model evaluation and A/B testing
- Quarterly feature engineering review (add new features)

---

## 6.3 GraphQL API

### Overview

Implement a GraphQL API layer parallel to the existing REST API, providing flexible, efficient data fetching for frontend applications and third-party integrations.

**Estimated Effort**: 3-4 weeks
**Complexity**: Medium-High

### Rationale

**Why GraphQL**:
- **Flexible Queries**: Clients request exactly the data they need
- **Single Request**: Fetch related data in one query (no over/under-fetching)
- **Strong Typing**: Auto-generated API documentation
- **Real-Time**: Subscriptions for live updates (bot scores, events, chat)
- **Developer Experience**: GraphQL Playground for exploration

**Parallel to REST**:
- Maintain existing REST endpoints for backward compatibility
- GraphQL for modern frontends (React, Vue, mobile apps)
- Both APIs share the same business logic services

### Technology Stack

**Framework**: Strawberry GraphQL (Python, async/await support)
```txt
strawberry-graphql==0.220.0
strawberry-graphql-fastapi==0.1.0    # FastAPI integration
```

**Alternative**: Graphene (older, more mature, less modern syntax)

### Schema Design

#### Core Types

```python
# graphql/types/user.py
import strawberry
from typing import Optional, List
from datetime import datetime

@strawberry.type
class User:
    id: int
    hub_user_id: int
    username: str
    email: Optional[str]
    avatar_url: Optional[str]
    created_at: datetime

    # Linked identities
    @strawberry.field
    async def identities(self, info) -> List['Identity']:
        return await info.context.identity_service.get_user_identities(self.hub_user_id)

    # Reputation
    @strawberry.field
    async def reputation(self, info) -> Optional['Reputation']:
        return await info.context.reputation_service.get_reputation(self.hub_user_id)

@strawberry.type
class Identity:
    id: int
    platform: str
    platform_user_id: str
    platform_username: str
    verified: bool
    linked_at: datetime

@strawberry.type
class Reputation:
    score: int
    level: int
    rank: str
    total_messages: int
    last_active: datetime
```

```python
# graphql/types/community.py
@strawberry.type
class Community:
    id: int
    name: str
    description: Optional[str]
    platform: str
    member_count: int
    created_at: datetime

    @strawberry.field
    async def bot_score(self, info) -> Optional['BotScore']:
        return await info.context.bot_score_service.get_score(self.id)

    @strawberry.field
    async def events(
        self,
        info,
        limit: int = 10,
        upcoming_only: bool = True
    ) -> List['Event']:
        return await info.context.calendar_service.list_events(
            self.id, limit=limit, upcoming_only=upcoming_only
        )

@strawberry.type
class BotScore:
    community_id: int
    overall_score: int
    grade: str
    size_category: str
    component_scores: strawberry.scalars.JSON
    calculated_at: datetime

    @strawberry.field
    async def suspected_bots(
        self,
        info,
        min_confidence: int = 50
    ) -> List['SuspectedBot']:
        return await info.context.bot_score_service.get_suspected_bots(
            self.community_id, min_confidence=min_confidence
        )

@strawberry.type
class SuspectedBot:
    hub_user_id: int
    platform_username: str
    confidence_score: int
    bot_indicators: strawberry.scalars.JSON
    detected_at: datetime
    is_false_positive: Optional[bool]
```

```python
# graphql/types/calendar.py
@strawberry.type
class Event:
    id: int
    event_uuid: str
    title: str
    description: Optional[str]
    event_date: datetime
    end_date: Optional[datetime]
    timezone: str
    location: Optional[str]
    status: str
    attending_count: int
    interested_count: int

    @strawberry.field
    async def rsvps(self, info, status: Optional[str] = None) -> List['RSVP']:
        return await info.context.rsvp_service.get_rsvps(self.id, status=status)

    @strawberry.field
    async def platform_syncs(self, info) -> List['EventPlatformSync']:
        return [
            EventPlatformSync(
                platform='discord',
                event_id=self.discord_event_id,
                synced=bool(self.discord_event_id)
            ),
            EventPlatformSync(
                platform='twitch',
                event_id=self.twitch_segment_id,
                synced=bool(self.twitch_segment_id)
            )
        ]

@strawberry.type
class RSVP:
    user_id: int
    event_id: int
    status: str  # attending, interested, declined
    created_at: datetime

@strawberry.type
class EventPlatformSync:
    platform: str
    event_id: Optional[str]
    synced: bool
```

#### Query Root

```python
# graphql/queries.py
import strawberry
from typing import Optional, List

@strawberry.type
class Query:
    @strawberry.field
    async def user(self, info, hub_user_id: int) -> Optional[User]:
        """Get user by hub_user_id"""
        return await info.context.user_service.get_user(hub_user_id)

    @strawberry.field
    async def me(self, info) -> Optional[User]:
        """Get current authenticated user"""
        user = info.context.current_user
        if not user:
            raise PermissionError("Not authenticated")
        return user

    @strawberry.field
    async def community(self, info, community_id: int) -> Optional[Community]:
        """Get community by ID"""
        return await info.context.community_service.get_community(community_id)

    @strawberry.field
    async def communities(
        self,
        info,
        limit: int = 20,
        offset: int = 0
    ) -> List[Community]:
        """List communities (for current user)"""
        user = info.context.current_user
        return await info.context.community_service.list_user_communities(
            user.id, limit=limit, offset=offset
        )

    @strawberry.field
    async def events(
        self,
        info,
        community_id: int,
        upcoming_only: bool = True,
        limit: int = 50
    ) -> List[Event]:
        """List events for a community"""
        return await info.context.calendar_service.list_events(
            community_id, upcoming_only=upcoming_only, limit=limit
        )

    @strawberry.field
    async def event(self, info, event_id: int) -> Optional[Event]:
        """Get event by ID"""
        return await info.context.calendar_service.get_event(event_id)
```

#### Mutation Root

```python
# graphql/mutations.py
@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_event(
        self,
        info,
        community_id: int,
        title: str,
        event_date: datetime,
        description: Optional[str] = None,
        timezone: str = "UTC"
    ) -> Event:
        """Create a new calendar event"""
        user = info.context.current_user
        if not user:
            raise PermissionError("Not authenticated")

        event = await info.context.calendar_service.create_event({
            'community_id': community_id,
            'title': title,
            'description': description,
            'event_date': event_date,
            'timezone': timezone
        }, user_context=user)

        return event

    @strawberry.mutation
    async def rsvp_to_event(
        self,
        info,
        event_id: int,
        status: str  # attending, interested, declined
    ) -> RSVP:
        """RSVP to an event"""
        user = info.context.current_user
        if not user:
            raise PermissionError("Not authenticated")

        return await info.context.rsvp_service.create_rsvp(
            event_id=event_id,
            user_id=user.id,
            status=status
        )

    @strawberry.mutation
    async def mark_bot_reviewed(
        self,
        info,
        bot_id: int,
        community_id: int,
        is_false_positive: bool
    ) -> SuspectedBot:
        """Mark a suspected bot as reviewed"""
        user = info.context.current_user
        if not user or user.role not in ['admin', 'moderator']:
            raise PermissionError("Insufficient permissions")

        return await info.context.bot_score_service.mark_bot_reviewed(
            community_id=community_id,
            bot_id=bot_id,
            is_false_positive=is_false_positive,
            reviewer_id=user.id
        )
```

#### Subscription Root (Real-Time)

```python
# graphql/subscriptions.py
import strawberry
from typing import AsyncGenerator

@strawberry.type
class Subscription:
    @strawberry.subscription
    async def bot_score_updated(
        self,
        info,
        community_id: int
    ) -> AsyncGenerator[BotScore, None]:
        """Subscribe to bot score updates for a community"""
        # Use Redis pub/sub for real-time updates
        redis = info.context.redis
        pubsub = redis.pubsub()
        await pubsub.subscribe(f'bot_score:{community_id}')

        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    score = await info.context.bot_score_service.get_score(community_id)
                    yield score
        finally:
            await pubsub.unsubscribe(f'bot_score:{community_id}')

    @strawberry.subscription
    async def event_created(
        self,
        info,
        community_id: int
    ) -> AsyncGenerator[Event, None]:
        """Subscribe to new events in a community"""
        redis = info.context.redis
        pubsub = redis.pubsub()
        await pubsub.subscribe(f'events:{community_id}')

        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    event_id = int(message['data'])
                    event = await info.context.calendar_service.get_event(event_id)
                    yield event
        finally:
            await pubsub.unsubscribe(f'events:{community_id}')

    @strawberry.subscription
    async def chat_message(
        self,
        info,
        community_id: int,
        platform: Optional[str] = None
    ) -> AsyncGenerator['ChatMessage', None]:
        """Subscribe to live chat messages"""
        redis = info.context.redis
        channel = f'chat:{community_id}' if not platform else f'chat:{community_id}:{platform}'
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)

        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    yield ChatMessage.from_json(message['data'])
        finally:
            await pubsub.unsubscribe(channel)
```

### Resolver Implementation

```python
# graphql/context.py
from dataclasses import dataclass
from typing import Optional

@dataclass
class GraphQLContext:
    """Context passed to all resolvers"""
    current_user: Optional[User]

    # Service dependencies
    user_service: Any
    community_service: Any
    identity_service: Any
    reputation_service: Any
    calendar_service: Any
    rsvp_service: Any
    bot_score_service: Any

    # Infrastructure
    redis: Any
    dal: Any
    logger: Any

# graphql/app.py
from fastapi import FastAPI, Depends
from strawberry.fastapi import GraphQLRouter
import strawberry

from .queries import Query
from .mutations import Mutation
from .subscriptions import Subscription
from .context import GraphQLContext

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription
)

async def get_context(
    # Inject dependencies from FastAPI
    current_user = Depends(get_current_user),
    user_service = Depends(get_user_service),
    # ... other services
) -> GraphQLContext:
    return GraphQLContext(
        current_user=current_user,
        user_service=user_service,
        # ... other services
    )

graphql_app = GraphQLRouter(
    schema,
    context_getter=get_context,
    graphql_ide="playground"  # Enable GraphQL Playground UI
)

# Mount in hub_module or as separate service
app = FastAPI()
app.include_router(graphql_app, prefix="/graphql")
```

### Deployment Strategy

**Option 1**: Integrate into Hub Module
- Add `/graphql` endpoint to `admin/hub_module/backend/`
- Share authentication and services with REST API
- Simpler deployment, single codebase

**Option 2**: Separate GraphQL Service
- New service: `core/graphql_core_module/`
- Independent scaling and versioning
- More complex deployment, additional container

**Recommendation**: Start with Option 1 (Hub Module integration), migrate to Option 2 if GraphQL usage grows significantly.

### Client Examples

**React (Apollo Client)**:
```javascript
import { gql, useQuery, useSubscription } from '@apollo/client';

const GET_COMMUNITY_BOT_SCORE = gql`
  query GetCommunityBotScore($communityId: Int!) {
    community(communityId: $communityId) {
      id
      name
      botScore {
        overallScore
        grade
        componentScores
        suspectedBots(minConfidence: 70) {
          hubUserId
          platformUsername
          confidenceScore
          detectedAt
        }
      }
    }
  }
`;

function BotDetectionDashboard({ communityId }) {
  const { data, loading } = useQuery(GET_COMMUNITY_BOT_SCORE, {
    variables: { communityId }
  });

  if (loading) return <Loading />;

  return (
    <div>
      <h1>Bot Detection: Grade {data.community.botScore.grade}</h1>
      <ScoreChart score={data.community.botScore.overallScore} />
      <BotList bots={data.community.botScore.suspectedBots} />
    </div>
  );
}

// Real-time subscription
const BOT_SCORE_SUBSCRIPTION = gql`
  subscription OnBotScoreUpdated($communityId: Int!) {
    botScoreUpdated(communityId: $communityId) {
      overallScore
      grade
      calculatedAt
    }
  }
`;

function LiveBotScore({ communityId }) {
  const { data } = useSubscription(BOT_SCORE_SUBSCRIPTION, {
    variables: { communityId }
  });

  return <ScoreBadge score={data?.botScoreUpdated.overallScore} />;
}
```

### Performance Considerations

**N+1 Query Problem**:
- Use DataLoader pattern for batching database queries
- Strawberry DataLoader: `strawberry.dataloader`

```python
from strawberry.dataloader import DataLoader

async def load_users(keys: List[int]) -> List[User]:
    """Batch load users by IDs"""
    users = await dal.execute(
        "SELECT * FROM users WHERE id = ANY($1)",
        [keys]
    )
    user_map = {u['id']: u for u in users}
    return [user_map.get(k) for k in keys]

user_loader = DataLoader(load_fn=load_users)

# In resolver:
@strawberry.field
async def user(self, info) -> User:
    return await info.context.user_loader.load(self.user_id)
```

**Query Complexity**:
- Limit query depth (max 10 levels)
- Limit result set size (max 1000 items per query)
- Query cost analysis (assign cost to fields, reject expensive queries)

**Caching**:
- Use Redis for caching resolver results
- Cache entire query responses (cache key = query hash + variables)
- Invalidate cache on mutations

### Monitoring

**Metrics to Track**:
- Query execution time (p50, p95, p99)
- Query complexity distribution
- Most frequent queries (optimize these)
- Error rate
- Subscription connection count

**Logging**:
- Log all GraphQL errors with query context
- Slow query logging (>1 second)
- Authentication failures

---

## 6.4 Advanced Calendar Features

### Overview

Enhance the existing calendar module with platform synchronization, recurring events (RRULE), multi-channel reminder system, and iCal export.

**Current State**: Basic event CRUD exists (`action/interactive/calendar_interaction_module/`)
**Estimated Effort**: 3-4 weeks

### Feature 1: Platform Sync (Discord, Twitch, Slack)

#### Discord Events API Integration

**Discord Events**: Discord has native scheduled events
- Create Discord event when Waddles event created
- Sync RSVP status bidirectionally
- Update/delete Discord event when Waddles event changes

```python
# calendar_interaction_module/services/platform_sync/discord_sync.py
import discord
from datetime import datetime
from typing import Optional, Dict, Any

class DiscordEventSync:
    """
    Sync Waddles calendar events with Discord scheduled events.

    Discord API: https://discord.com/developers/docs/resources/guild-scheduled-event
    """

    def __init__(self, discord_client, dal, logger):
        self.client = discord_client
        self.dal = dal
        self.logger = logger

    async def create_discord_event(
        self,
        event: Dict[str, Any],
        guild_id: str
    ) -> Optional[str]:
        """
        Create Discord scheduled event.

        Returns: Discord event ID or None on failure
        """
        try:
            guild = await self.client.fetch_guild(int(guild_id))

            # Map Waddles event to Discord event
            scheduled_event = await guild.create_scheduled_event(
                name=event['title'],
                description=event['description'][:1000] if event['description'] else None,
                start_time=event['event_date'],
                end_time=event['end_date'] if event['end_date'] else event['event_date'] + timedelta(hours=1),
                entity_type=discord.EntityType.external,
                location=event['location'] or "See event details",
                privacy_level=discord.PrivacyLevel.guild_only
            )

            # Store Discord event ID in Waddles database
            await self.dal.execute(
                "UPDATE calendar_events SET discord_event_id = $1, sync_status = 'synced' WHERE id = $2",
                [str(scheduled_event.id), event['id']]
            )

            self.logger.audit(
                "Discord event created",
                event_id=event['id'],
                discord_event_id=scheduled_event.id,
                result="SUCCESS"
            )

            return str(scheduled_event.id)

        except Exception as e:
            self.logger.error(f"Failed to create Discord event: {e}", event_id=event['id'])
            await self.dal.execute(
                "UPDATE calendar_events SET sync_status = 'failed', sync_error = $1 WHERE id = $2",
                [str(e), event['id']]
            )
            return None

    async def sync_rsvps(self, event_id: int, discord_event_id: str):
        """Sync RSVP status from Discord to Waddles"""
        try:
            # Get Discord event and interested users
            scheduled_event = await self.client.fetch_scheduled_event(discord_event_id)
            users = await scheduled_event.fetch_users(limit=1000)

            # Map Discord users to Waddles users via identity_core_module
            for user in users:
                hub_user_id = await self._get_hub_user_id(user.id, 'discord')
                if hub_user_id:
                    # Create RSVP if not exists
                    await self.dal.execute("""
                        INSERT INTO calendar_rsvps (event_id, user_id, status, created_at)
                        VALUES ($1, $2, 'interested', NOW())
                        ON CONFLICT (event_id, user_id) DO NOTHING
                    """, [event_id, hub_user_id])

            self.logger.audit(
                "Discord RSVPs synced",
                event_id=event_id,
                discord_event_id=discord_event_id,
                count=len(users),
                result="SUCCESS"
            )

        except Exception as e:
            self.logger.error(f"Failed to sync Discord RSVPs: {e}", event_id=event_id)

    async def _get_hub_user_id(self, discord_user_id: str, platform: str) -> Optional[int]:
        """Look up Waddles hub_user_id from platform identity"""
        rows = await self.dal.execute(
            "SELECT hub_user_id FROM identity_links WHERE platform = $1 AND platform_user_id = $2",
            [platform, discord_user_id]
        )
        return rows[0]['hub_user_id'] if rows else None
```

#### Twitch Stream Schedule Integration

**Twitch Schedule API**: Twitch has stream schedule segments
- Sync Waddles events to Twitch schedule
- Display upcoming streams in Waddles calendar

```python
# calendar_interaction_module/services/platform_sync/twitch_sync.py
import httpx
from datetime import datetime
from typing import Optional, Dict, Any

class TwitchScheduleSync:
    """
    Sync Waddles calendar events with Twitch stream schedule.

    Twitch API: https://dev.twitch.tv/docs/api/reference#create-channel-stream-schedule-segment
    """

    def __init__(self, twitch_api_client, dal, logger):
        self.api = twitch_api_client
        self.dal = dal
        self.logger = logger

    async def create_stream_segment(
        self,
        event: Dict[str, Any],
        broadcaster_id: str
    ) -> Optional[str]:
        """
        Create Twitch stream schedule segment.

        Returns: Segment ID or None on failure
        """
        try:
            # Calculate duration in minutes
            duration = 60  # default 1 hour
            if event.get('end_date'):
                duration = int((event['end_date'] - event['event_date']).total_seconds() / 60)

            response = await self.api.post(
                'https://api.twitch.tv/helix/schedule/segment',
                params={
                    'broadcaster_id': broadcaster_id
                },
                json={
                    'start_time': event['event_date'].isoformat() + 'Z',
                    'timezone': event.get('timezone', 'UTC'),
                    'duration': str(duration),
                    'is_recurring': False,
                    'category_id': None,  # Could map to game category
                    'title': event['title']
                }
            )

            segment = response.json()['data']['segments'][0]
            segment_id = segment['id']

            # Store Twitch segment ID
            await self.dal.execute(
                "UPDATE calendar_events SET twitch_segment_id = $1, sync_status = 'synced' WHERE id = $2",
                [segment_id, event['id']]
            )

            self.logger.audit(
                "Twitch segment created",
                event_id=event['id'],
                segment_id=segment_id,
                result="SUCCESS"
            )

            return segment_id

        except Exception as e:
            self.logger.error(f"Failed to create Twitch segment: {e}", event_id=event['id'])
            return None
```

#### Slack Reminders Integration

**Slack Reminders**: No native calendar, but can post event announcements
- Post event creation announcement to Slack channel
- Schedule Slack reminders (1 hour, 15 min before event)

```python
# calendar_interaction_module/services/platform_sync/slack_sync.py
from slack_sdk.web.async_client import AsyncWebClient
from datetime import datetime, timedelta

class SlackEventSync:
    """Post calendar events and reminders to Slack channels"""

    def __init__(self, slack_client: AsyncWebClient, dal, logger):
        self.client = slack_client
        self.dal = dal
        self.logger = logger

    async def post_event_announcement(
        self,
        event: Dict[str, Any],
        channel_id: str
    ):
        """Post event announcement as Slack message with blocks"""
        try:
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"📅 {event['title']}"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*When:*\n{event['event_date'].strftime('%B %d, %Y at %I:%M %p')}"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Location:*\n{event.get('location', 'TBD')}"
                        }
                    ]
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": event.get('description', 'No description provided.')
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Interested"
                            },
                            "value": f"rsvp_{event['id']}_interested",
                            "action_id": "rsvp_interested"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Attending"
                            },
                            "value": f"rsvp_{event['id']}_attending",
                            "action_id": "rsvp_attending"
                        }
                    ]
                }
            ]

            response = await self.client.chat_postMessage(
                channel=channel_id,
                text=f"New event: {event['title']}",
                blocks=blocks
            )

            # Store Slack message timestamp
            await self.dal.execute(
                "UPDATE calendar_events SET slack_message_ts = $1 WHERE id = $2",
                [response['ts'], event['id']]
            )

            self.logger.audit(
                "Slack event posted",
                event_id=event['id'],
                channel=channel_id,
                result="SUCCESS"
            )

        except Exception as e:
            self.logger.error(f"Failed to post Slack event: {e}", event_id=event['id'])
```

### Feature 2: Recurring Events (RRULE)

**RFC 5545 (iCalendar) Recurrence Rules**: Industry standard for recurring events

```python
# calendar_interaction_module/services/recurring_events.py
from dateutil.rrule import rrule, DAILY, WEEKLY, MONTHLY, YEARLY
from datetime import datetime, timedelta
from typing import List, Dict, Any

class RecurringEventService:
    """
    Manage recurring events using RFC 5545 RRULE.

    Libraries: python-dateutil (rrule), icalendar (full iCal support)
    """

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def create_recurring_event(
        self,
        event_data: Dict[str, Any],
        recurrence_rule: str,
        end_date: datetime
    ) -> List[int]:
        """
        Create recurring event series.

        Args:
            event_data: Base event data
            recurrence_rule: RRULE string (e.g., "FREQ=WEEKLY;BYDAY=MO,WE,FR")
            end_date: When recurrence ends

        Returns: List of created event IDs
        """
        try:
            # Parse RRULE
            dtstart = event_data['event_date']
            rule = rrule.rrulestr(recurrence_rule, dtstart=dtstart)

            # Generate occurrences
            occurrences = []
            for dt in rule:
                if dt > end_date:
                    break
                occurrences.append(dt)

            # Create parent event
            parent_id = await self._create_parent_event(event_data, recurrence_rule, end_date)

            # Create child events for each occurrence
            event_ids = []
            for occurrence_date in occurrences:
                child_id = await self._create_child_event(
                    event_data,
                    occurrence_date,
                    parent_id
                )
                event_ids.append(child_id)

            self.logger.audit(
                "Recurring event series created",
                parent_id=parent_id,
                occurrences=len(event_ids),
                result="SUCCESS"
            )

            return event_ids

        except Exception as e:
            self.logger.error(f"Failed to create recurring event: {e}")
            raise

    async def _create_parent_event(
        self,
        event_data: Dict[str, Any],
        rrule: str,
        end_date: datetime
    ) -> int:
        """Create parent/template event for recurring series"""
        query = """
            INSERT INTO calendar_events (
                community_id, entity_id, platform, title, description,
                event_date, timezone, is_recurring, recurring_pattern,
                recurring_end_date, status, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8, $9, 'template', NOW())
            RETURNING id
        """
        rows = await self.dal.execute(
            query,
            [
                event_data['community_id'],
                event_data['entity_id'],
                event_data['platform'],
                event_data['title'],
                event_data.get('description'),
                event_data['event_date'],
                event_data.get('timezone', 'UTC'),
                rrule,
                end_date
            ]
        )
        return rows[0]['id']

    async def _create_child_event(
        self,
        event_data: Dict[str, Any],
        occurrence_date: datetime,
        parent_id: int
    ) -> int:
        """Create individual occurrence of recurring event"""
        query = """
            INSERT INTO calendar_events (
                community_id, entity_id, platform, title, description,
                event_date, timezone, parent_event_id, status, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', NOW())
            RETURNING id
        """
        rows = await self.dal.execute(
            query,
            [
                event_data['community_id'],
                event_data['entity_id'],
                event_data['platform'],
                event_data['title'],
                event_data.get('description'),
                occurrence_date,
                event_data.get('timezone', 'UTC'),
                parent_id
            ]
        )
        return rows[0]['id']

    def parse_human_recurrence(self, text: str) -> str:
        """
        Convert human-readable recurrence to RRULE.

        Examples:
        - "every day" -> FREQ=DAILY
        - "every week on Monday and Wednesday" -> FREQ=WEEKLY;BYDAY=MO,WE
        - "every 2 weeks" -> FREQ=WEEKLY;INTERVAL=2
        - "monthly on the 1st" -> FREQ=MONTHLY;BYMONTHDAY=1
        """
        text_lower = text.lower()

        if "every day" in text_lower or "daily" in text_lower:
            return "FREQ=DAILY"

        elif "every week" in text_lower or "weekly" in text_lower:
            # Check for specific days
            days_map = {
                'monday': 'MO', 'tuesday': 'TU', 'wednesday': 'WE',
                'thursday': 'TH', 'friday': 'FR', 'saturday': 'SA', 'sunday': 'SU'
            }
            days = [days_map[day] for day in days_map if day in text_lower]
            if days:
                return f"FREQ=WEEKLY;BYDAY={','.join(days)}"
            return "FREQ=WEEKLY"

        elif "every month" in text_lower or "monthly" in text_lower:
            return "FREQ=MONTHLY"

        elif "every year" in text_lower or "annually" in text_lower:
            return "FREQ=YEARLY"

        else:
            raise ValueError(f"Could not parse recurrence: {text}")
```

**Database Schema**:
```sql
-- Add recurring event fields (already exists in current schema)
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS is_recurring BOOLEAN DEFAULT FALSE;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS recurring_pattern TEXT;  -- RRULE string
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS recurring_days INTEGER[];  -- Day of week (0=Mon)
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS recurring_end_date TIMESTAMP;
ALTER TABLE calendar_events ADD COLUMN IF NOT EXISTS parent_event_id INTEGER REFERENCES calendar_events(id);
```

### Feature 3: Multi-Channel Reminder Notifications

**Reminder System**: Send notifications before events start
- Configurable reminder times (1 day, 1 hour, 15 min, starting now)
- Multi-platform delivery (Discord DM, Twitch whisper, Slack DM, email)
- User preferences (opt-in/opt-out per platform)

```python
# calendar_interaction_module/services/reminder_service.py
from datetime import datetime, timedelta
from typing import List, Dict, Any
import asyncio

class EventReminderService:
    """
    Schedule and send event reminders across platforms.

    Reminder schedule:
    - 24 hours before
    - 1 hour before
    - 15 minutes before
    - Event starting now
    """

    def __init__(self, dal, notification_service, logger):
        self.dal = dal
        self.notification = notification_service
        self.logger = logger

    async def schedule_reminders(self, event_id: int):
        """Schedule all reminders for an event"""
        try:
            # Get event details
            event = await self._get_event(event_id)
            if not event:
                return

            # Get RSVPed users
            users = await self._get_rsvp_users(event_id)

            # Calculate reminder times
            reminder_times = [
                (event['event_date'] - timedelta(days=1), '24 hours'),
                (event['event_date'] - timedelta(hours=1), '1 hour'),
                (event['event_date'] - timedelta(minutes=15), '15 minutes'),
                (event['event_date'], 'now')
            ]

            # Schedule reminder jobs
            for reminder_time, label in reminder_times:
                if reminder_time > datetime.utcnow():
                    await self._schedule_reminder_job(
                        event_id=event_id,
                        users=users,
                        reminder_time=reminder_time,
                        label=label
                    )

            self.logger.audit(
                "Event reminders scheduled",
                event_id=event_id,
                user_count=len(users),
                reminder_count=len(reminder_times),
                result="SUCCESS"
            )

        except Exception as e:
            self.logger.error(f"Failed to schedule reminders: {e}", event_id=event_id)

    async def _schedule_reminder_job(
        self,
        event_id: int,
        users: List[Dict],
        reminder_time: datetime,
        label: str
    ):
        """
        Schedule reminder job using Celery or APScheduler.

        Note: This requires a background job system.
        """
        # Store reminder in database
        await self.dal.execute("""
            INSERT INTO calendar_reminders (event_id, reminder_time, label, status)
            VALUES ($1, $2, $3, 'scheduled')
        """, [event_id, reminder_time, label])

        # In production, use Celery:
        # send_event_reminder.apply_async(
        #     args=[event_id, label],
        #     eta=reminder_time
        # )

        # For now, store in database and use cron job to check

    async def send_reminder(self, event_id: int, label: str):
        """Send reminder to all RSVPed users"""
        try:
            event = await self._get_event(event_id)
            users = await self._get_rsvp_users(event_id)

            message = self._format_reminder_message(event, label)

            # Send to each user on their preferred platforms
            for user in users:
                await self._send_user_reminder(user, message, event)

            # Update reminder status
            await self.dal.execute("""
                UPDATE calendar_reminders
                SET status = 'sent', sent_at = NOW()
                WHERE event_id = $1 AND label = $2
            """, [event_id, label])

            self.logger.audit(
                "Event reminder sent",
                event_id=event_id,
                label=label,
                user_count=len(users),
                result="SUCCESS"
            )

        except Exception as e:
            self.logger.error(f"Failed to send reminder: {e}", event_id=event_id)

    async def _send_user_reminder(
        self,
        user: Dict,
        message: str,
        event: Dict
    ):
        """Send reminder to user via their active platforms"""
        # Get user notification preferences
        prefs = await self._get_notification_prefs(user['hub_user_id'])

        # Send via enabled platforms
        tasks = []
        if prefs.get('discord_enabled'):
            tasks.append(self.notification.send_discord_dm(user, message))
        if prefs.get('twitch_enabled'):
            tasks.append(self.notification.send_twitch_whisper(user, message))
        if prefs.get('slack_enabled'):
            tasks.append(self.notification.send_slack_dm(user, message))
        if prefs.get('email_enabled') and user.get('email'):
            tasks.append(self.notification.send_email(user['email'], event, message))

        # Send in parallel
        await asyncio.gather(*tasks, return_exceptions=True)

    def _format_reminder_message(self, event: Dict, label: str) -> str:
        """Format reminder message"""
        if label == 'now':
            return f"🔴 {event['title']} is starting NOW! {event.get('location', '')}"
        else:
            return f"⏰ Reminder: {event['title']} starts in {label}! {event.get('location', '')}"

    async def _get_notification_prefs(self, hub_user_id: int) -> Dict:
        """Get user notification preferences"""
        rows = await self.dal.execute(
            "SELECT * FROM user_notification_prefs WHERE hub_user_id = $1",
            [hub_user_id]
        )
        return rows[0] if rows else {
            'discord_enabled': True,
            'twitch_enabled': True,
            'slack_enabled': True,
            'email_enabled': False
        }
```

**Database Schema**:
```sql
CREATE TABLE calendar_reminders (
    id SERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES calendar_events(id) ON DELETE CASCADE,
    reminder_time TIMESTAMP NOT NULL,
    label VARCHAR(50) NOT NULL,  -- '24 hours', '1 hour', '15 minutes', 'now'
    status VARCHAR(50) DEFAULT 'scheduled',  -- scheduled, sent, failed
    sent_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_reminders_time ON calendar_reminders(reminder_time) WHERE status = 'scheduled';

CREATE TABLE user_notification_prefs (
    hub_user_id INTEGER PRIMARY KEY,
    discord_enabled BOOLEAN DEFAULT TRUE,
    twitch_enabled BOOLEAN DEFAULT TRUE,
    slack_enabled BOOLEAN DEFAULT TRUE,
    email_enabled BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT NOW()
);
```

**Cron Job** (check every minute for pending reminders):
```bash
# calendar_interaction_module/jobs/send_reminders.sh
*/1 * * * * python /app/jobs/check_reminders.py
```

```python
# calendar_interaction_module/jobs/check_reminders.py
"""
Cron job to check and send pending reminders.
Run every minute.
"""
import asyncio
from datetime import datetime
from services.reminder_service import EventReminderService

async def main():
    # Get reminders due now (within last minute)
    reminders = await dal.execute("""
        SELECT event_id, label
        FROM calendar_reminders
        WHERE status = 'scheduled'
        AND reminder_time <= NOW()
        AND reminder_time >= NOW() - INTERVAL '1 minute'
    """)

    # Send each reminder
    reminder_service = EventReminderService(dal, notification_service, logger)
    for reminder in reminders:
        await reminder_service.send_reminder(
            reminder['event_id'],
            reminder['label']
        )

if __name__ == '__main__':
    asyncio.run(main())
```

### Feature 4: iCal Export

**iCalendar Format**: Standard calendar format (.ics files)
- Export community events to .ics file
- Import into Google Calendar, Outlook, Apple Calendar
- Subscribe to live calendar feed (webcal://)

```python
# calendar_interaction_module/services/ical_service.py
from icalendar import Calendar, Event as ICalEvent, vDatetime
from datetime import datetime
from typing import List, Dict, Any

class ICalExportService:
    """Export events to iCalendar (.ics) format"""

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    def export_events_to_ical(
        self,
        events: List[Dict[str, Any]],
        community_name: str
    ) -> str:
        """
        Export events to iCalendar format.

        Returns: iCal file content (string)
        """
        cal = Calendar()
        cal.add('prodid', '-//Waddles//Calendar//EN')
        cal.add('version', '2.0')
        cal.add('x-wr-calname', f'{community_name} Events')
        cal.add('x-wr-timezone', 'UTC')

        for event_data in events:
            event = ICalEvent()
            event.add('uid', f'waddlebot-{event_data["event_uuid"]}')
            event.add('summary', event_data['title'])
            event.add('description', event_data.get('description', ''))
            event.add('dtstart', vDatetime(event_data['event_date']))

            if event_data.get('end_date'):
                event.add('dtend', vDatetime(event_data['end_date']))
            else:
                # Default 1 hour duration
                event.add('dtend', vDatetime(event_data['event_date'] + timedelta(hours=1)))

            if event_data.get('location'):
                event.add('location', event_data['location'])

            # Add RRULE if recurring
            if event_data.get('is_recurring') and event_data.get('recurring_pattern'):
                event.add('rrule', event_data['recurring_pattern'])

            # Add URL to Waddles event page
            event.add('url', f'https://hub.waddlebot.com/events/{event_data["event_uuid"]}')

            # Organizer
            if event_data.get('created_by_username'):
                event.add('organizer', f'CN={event_data["created_by_username"]}')

            # Created/Updated timestamps
            event.add('dtstamp', vDatetime(event_data['created_at']))
            event.add('last-modified', vDatetime(event_data['updated_at']))

            # Status
            status_map = {
                'pending': 'TENTATIVE',
                'approved': 'CONFIRMED',
                'cancelled': 'CANCELLED'
            }
            event.add('status', status_map.get(event_data['status'], 'TENTATIVE'))

            cal.add_component(event)

        return cal.to_ical().decode('utf-8')

    async def generate_ical_feed(self, community_id: int) -> str:
        """
        Generate iCal feed for a community.

        Returns: iCal file content
        """
        try:
            # Get community info
            community = await self._get_community(community_id)

            # Get upcoming approved events
            events = await self._get_community_events(
                community_id,
                status='approved',
                upcoming_only=True,
                limit=100
            )

            ical_content = self.export_events_to_ical(
                events,
                community['name']
            )

            self.logger.audit(
                "iCal feed generated",
                community_id=community_id,
                event_count=len(events),
                result="SUCCESS"
            )

            return ical_content

        except Exception as e:
            self.logger.error(f"Failed to generate iCal feed: {e}", community_id=community_id)
            raise
```

**API Endpoint** (add to `calendar_interaction_module/app.py`):
```python
@app.route('/api/calendar/ical/<int:community_id>', methods=['GET'])
async def get_ical_feed(community_id: int):
    """
    Get iCal feed for a community.

    Usage: https://waddlebot.com/api/calendar/ical/123
    Subscribe: webcal://waddlebot.com/api/calendar/ical/123
    """
    try:
        ical_service = ICalExportService(dal, logger)
        ical_content = await ical_service.generate_ical_feed(community_id)

        return Response(
            ical_content,
            mimetype='text/calendar',
            headers={
                'Content-Disposition': f'attachment; filename="waddlebot-{community_id}.ics"',
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500
```

**Frontend Integration**:
```javascript
// Display subscribe link in calendar UI
function CalendarSubscribe({ communityId }) {
  const icalUrl = `https://waddlebot.com/api/calendar/ical/${communityId}`;
  const webcalUrl = icalUrl.replace('https://', 'webcal://');

  return (
    <div className="calendar-subscribe">
      <h3>Subscribe to Calendar</h3>
      <p>Add this calendar to:</p>
      <ul>
        <li><a href={webcalUrl}>Apple Calendar</a></li>
        <li><a href={`https://calendar.google.com/calendar/r?cid=${encodeURIComponent(icalUrl)}`}>Google Calendar</a></li>
        <li><a href={icalUrl} download>Download .ics file</a> (Outlook)</li>
      </ul>
    </div>
  );
}
```

### Dependencies

```txt
# Add to calendar_interaction_module/requirements.txt
python-dateutil==2.8.2        # RRULE parsing
icalendar==5.0.11             # iCal export
py-cord==2.5.0                # Discord API (if not already)
aiohttp==3.9.0                # Twitch API
slack-sdk==3.26.0             # Slack API
```

---

## 6.5 Enhanced Loyalty System

### Overview

Expand the existing loyalty/currency system with economy balancing dashboard, tournament system, gear crafting, and anti-abuse measures.

**Current State**: Basic currency and gear systems exist (`action/interactive/loyalty_interaction_module/`)
**Estimated Effort**: 3-4 weeks

### Feature 1: Economy Balancing Dashboard

**Purpose**: Give community managers visibility into currency health and tools to balance the economy

```python
# loyalty_interaction_module/services/economy_dashboard.py
from datetime import datetime, timedelta
from typing import Dict, Any, List

class EconomyDashboardService:
    """
    Economy health monitoring and balancing tools.

    Tracks:
    - Currency supply (total in circulation)
    - Currency distribution (wealth inequality)
    - Currency velocity (transaction rate)
    - Inflation/deflation trends
    - Top earners and spenders
    """

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def get_economy_health(self, community_id: int) -> Dict[str, Any]:
        """Get comprehensive economy health metrics"""
        try:
            # Total currency in circulation
            total_supply = await self._get_total_supply(community_id)

            # Currency distribution (Gini coefficient for inequality)
            gini_coefficient = await self._calculate_gini_coefficient(community_id)

            # Velocity (transactions per day)
            velocity = await self._calculate_velocity(community_id)

            # Inflation rate (currency created vs destroyed)
            inflation_rate = await self._calculate_inflation_rate(community_id)

            # Top holders
            top_holders = await self._get_top_holders(community_id, limit=10)

            # Recent transactions
            transaction_volume = await self._get_transaction_volume(community_id, days=7)

            return {
                'total_supply': total_supply,
                'gini_coefficient': gini_coefficient,  # 0 = perfect equality, 1 = perfect inequality
                'inequality_level': self._interpret_gini(gini_coefficient),
                'velocity': velocity,
                'inflation_rate': inflation_rate,
                'top_holders': top_holders,
                'transaction_volume_7d': transaction_volume,
                'health_score': self._calculate_health_score(
                    gini_coefficient,
                    velocity,
                    inflation_rate
                ),
                'recommendations': self._generate_recommendations(
                    gini_coefficient,
                    velocity,
                    inflation_rate,
                    total_supply
                )
            }
        except Exception as e:
            self.logger.error(f"Failed to get economy health: {e}", community_id=community_id)
            raise

    async def _calculate_gini_coefficient(self, community_id: int) -> float:
        """
        Calculate Gini coefficient for wealth inequality.

        0 = perfect equality (everyone has same balance)
        1 = perfect inequality (one person has all currency)
        """
        rows = await self.dal.execute("""
            SELECT balance
            FROM loyalty_balances
            WHERE community_id = $1
            ORDER BY balance ASC
        """, [community_id])

        if not rows or len(rows) < 2:
            return 0.0

        balances = [row['balance'] for row in rows]
        n = len(balances)

        # Calculate Gini coefficient
        cumsum = sum((i + 1) * balance for i, balance in enumerate(balances))
        total = sum(balances)

        if total == 0:
            return 0.0

        gini = (2 * cumsum) / (n * total) - (n + 1) / n
        return round(gini, 3)

    def _interpret_gini(self, gini: float) -> str:
        """Interpret Gini coefficient"""
        if gini < 0.3:
            return 'low (healthy equality)'
        elif gini < 0.5:
            return 'moderate (acceptable)'
        elif gini < 0.7:
            return 'high (wealth concentrated)'
        else:
            return 'very high (needs intervention)'

    async def _calculate_inflation_rate(self, community_id: int, days: int = 30) -> float:
        """
        Calculate inflation rate (% change in supply).

        Positive = inflation (more currency created)
        Negative = deflation (more currency destroyed)
        """
        rows = await self.dal.execute("""
            SELECT
                SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as created,
                SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as destroyed
            FROM loyalty_transactions
            WHERE community_id = $1
            AND created_at >= NOW() - INTERVAL '$2 days'
        """, [community_id, days])

        created = rows[0]['created'] or 0
        destroyed = rows[0]['destroyed'] or 0

        if destroyed == 0:
            return 100.0  # Infinite inflation

        inflation_rate = ((created - destroyed) / destroyed) * 100
        return round(inflation_rate, 2)

    def _calculate_health_score(
        self,
        gini: float,
        velocity: float,
        inflation: float
    ) -> int:
        """
        Calculate overall economy health score (0-100).

        Good economy:
        - Moderate inequality (Gini 0.3-0.5)
        - Healthy velocity (5-20 tx/day per 100 users)
        - Low inflation (-5% to +10%)
        """
        score = 100

        # Gini penalty (0.4 is ideal)
        gini_penalty = abs(gini - 0.4) * 50
        score -= gini_penalty

        # Velocity penalty (10 is ideal)
        velocity_penalty = abs(velocity - 10) * 2
        score -= min(velocity_penalty, 30)

        # Inflation penalty (5% is ideal)
        inflation_penalty = abs(inflation - 5) * 1.5
        score -= min(inflation_penalty, 30)

        return max(0, min(100, int(score)))

    def _generate_recommendations(
        self,
        gini: float,
        velocity: float,
        inflation: float,
        total_supply: int
    ) -> List[str]:
        """Generate actionable recommendations"""
        recommendations = []

        if gini > 0.6:
            recommendations.append(
                "⚠️ High wealth inequality detected. Consider: "
                "(1) Reduce earning caps for top holders, "
                "(2) Implement progressive taxation on large balances, "
                "(3) Create events that reward smaller accounts more."
            )

        if velocity < 5:
            recommendations.append(
                "📉 Low transaction velocity. Currency is hoarded. Consider: "
                "(1) Add limited-time shop items, "
                "(2) Create regular mini-games/gambling, "
                "(3) Increase decay rate on idle balances."
            )

        if inflation > 20:
            recommendations.append(
                "💸 High inflation detected. Too much currency created. Consider: "
                "(1) Reduce earning rates, "
                "(2) Add more currency sinks (expensive items), "
                "(3) Implement transaction fees."
            )

        if inflation < -10:
            recommendations.append(
                "📊 Deflation detected. Currency supply shrinking. Consider: "
                "(1) Increase earning rates, "
                "(2) Reduce item costs, "
                "(3) Give bonus rewards for activity."
            )

        if not recommendations:
            recommendations.append("✅ Economy is healthy! No immediate actions needed.")

        return recommendations
```

### Feature 2: Tournament System

**Tournament Types**:
- **Brackets**: Single/double elimination
- **Leaderboard**: Top N winners
- **Battle Royale**: Last person standing

```python
# loyalty_interaction_module/services/tournament_service.py
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from enum import Enum

class TournamentType(Enum):
    BRACKET_SINGLE = 'bracket_single'
    BRACKET_DOUBLE = 'bracket_double'
    LEADERBOARD = 'leaderboard'
    BATTLE_ROYALE = 'battle_royale'

class TournamentService:
    """
    Tournament and competition system.

    Features:
    - Multiple tournament formats
    - Entry fees and prize pools
    - Automated bracket generation
    - Real-time leaderboard updates
    - Spectator mode
    """

    def __init__(self, dal, currency_service, logger):
        self.dal = dal
        self.currency = currency_service
        self.logger = logger

    async def create_tournament(
        self,
        community_id: int,
        name: str,
        tournament_type: TournamentType,
        entry_fee: int,
        prize_pool_distribution: List[float],  # [0.5, 0.3, 0.2] for 1st, 2nd, 3rd
        max_participants: Optional[int] = None,
        start_time: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Create new tournament"""
        try:
            tournament_id = await self.dal.execute("""
                INSERT INTO loyalty_tournaments (
                    community_id, name, tournament_type, entry_fee,
                    prize_distribution, max_participants, start_time,
                    status, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'registration', NOW())
                RETURNING id
            """, [
                community_id,
                name,
                tournament_type.value,
                entry_fee,
                prize_pool_distribution,
                max_participants,
                start_time or datetime.utcnow() + timedelta(hours=1)
            ])

            tournament = {
                'id': tournament_id[0]['id'],
                'name': name,
                'type': tournament_type.value,
                'entry_fee': entry_fee,
                'status': 'registration'
            }

            self.logger.audit(
                "Tournament created",
                community_id=community_id,
                tournament_id=tournament['id'],
                result="SUCCESS"
            )

            return tournament

        except Exception as e:
            self.logger.error(f"Failed to create tournament: {e}", community_id=community_id)
            raise

    async def register_participant(
        self,
        tournament_id: int,
        user_id: int,
        community_id: int
    ) -> bool:
        """Register user for tournament (charges entry fee)"""
        try:
            # Get tournament info
            tournament = await self._get_tournament(tournament_id)

            if tournament['status'] != 'registration':
                raise ValueError("Tournament registration closed")

            # Check max participants
            if tournament['max_participants']:
                current_count = await self._get_participant_count(tournament_id)
                if current_count >= tournament['max_participants']:
                    raise ValueError("Tournament full")

            # Charge entry fee
            if tournament['entry_fee'] > 0:
                success = await self.currency.deduct_currency(
                    community_id=community_id,
                    user_id=user_id,
                    amount=tournament['entry_fee'],
                    reason=f"Tournament entry: {tournament['name']}"
                )
                if not success:
                    raise ValueError("Insufficient balance for entry fee")

            # Register participant
            await self.dal.execute("""
                INSERT INTO loyalty_tournament_participants (
                    tournament_id, user_id, entry_fee_paid, registered_at
                ) VALUES ($1, $2, $3, NOW())
            """, [tournament_id, user_id, tournament['entry_fee']])

            self.logger.audit(
                "Tournament registration",
                tournament_id=tournament_id,
                user_id=user_id,
                entry_fee=tournament['entry_fee'],
                result="SUCCESS"
            )

            return True

        except Exception as e:
            self.logger.error(f"Failed to register participant: {e}", tournament_id=tournament_id)
            return False

    async def start_tournament(self, tournament_id: int):
        """Start tournament (generate brackets, initialize leaderboard)"""
        try:
            tournament = await self._get_tournament(tournament_id)
            participants = await self._get_participants(tournament_id)

            if tournament['tournament_type'] == TournamentType.BRACKET_SINGLE.value:
                await self._generate_single_elimination_bracket(tournament_id, participants)
            elif tournament['tournament_type'] == TournamentType.LEADERBOARD.value:
                await self._initialize_leaderboard(tournament_id, participants)

            # Update status
            await self.dal.execute("""
                UPDATE loyalty_tournaments
                SET status = 'active', started_at = NOW()
                WHERE id = $1
            """, [tournament_id])

            self.logger.audit(
                "Tournament started",
                tournament_id=tournament_id,
                participant_count=len(participants),
                result="SUCCESS"
            )

        except Exception as e:
            self.logger.error(f"Failed to start tournament: {e}", tournament_id=tournament_id)
            raise

    async def _generate_single_elimination_bracket(
        self,
        tournament_id: int,
        participants: List[Dict]
    ):
        """Generate single elimination bracket"""
        import random

        # Shuffle participants
        shuffled = participants.copy()
        random.shuffle(shuffled)

        # Calculate number of rounds (log2)
        import math
        num_rounds = math.ceil(math.log2(len(shuffled)))

        # Generate first round matches
        for i in range(0, len(shuffled), 2):
            if i + 1 < len(shuffled):
                await self.dal.execute("""
                    INSERT INTO loyalty_tournament_matches (
                        tournament_id, round_number, participant1_id, participant2_id,
                        status, created_at
                    ) VALUES ($1, 1, $2, $3, 'pending', NOW())
                """, [tournament_id, shuffled[i]['id'], shuffled[i+1]['id']])
            else:
                # Bye (participant advances automatically)
                await self.dal.execute("""
                    INSERT INTO loyalty_tournament_matches (
                        tournament_id, round_number, participant1_id, winner_id,
                        status, created_at
                    ) VALUES ($1, 1, $2, $2, 'completed', NOW())
                """, [tournament_id, shuffled[i]['id']])
```

### Feature 3: Gear Crafting System

**Crafting**: Combine lower-tier items to create higher-tier items

```python
# loyalty_interaction_module/services/crafting_service.py
from typing import List, Dict, Any, Optional

class CraftingService:
    """
    Gear crafting system.

    Features:
    - Recipes (combine items to create new items)
    - Success probability (can fail)
    - Resource requirements (currency + items)
    - Crafting cooldowns
    - Rare item drops
    """

    def __init__(self, dal, gear_service, currency_service, logger):
        self.dal = dal
        self.gear = gear_service
        self.currency = currency_service
        self.logger = logger

    async def craft_item(
        self,
        community_id: int,
        user_id: int,
        recipe_id: int
    ) -> Dict[str, Any]:
        """Attempt to craft an item using a recipe"""
        try:
            # Get recipe
            recipe = await self._get_recipe(recipe_id, community_id)

            # Check cooldown
            if not await self._check_cooldown(user_id, recipe_id):
                raise ValueError("Crafting cooldown active")

            # Check user has required ingredients
            if not await self._has_ingredients(user_id, recipe['ingredients']):
                raise ValueError("Missing required ingredients")

            # Check currency requirement
            if recipe['currency_cost'] > 0:
                balance = await self.currency.get_balance(community_id, user_id)
                if balance < recipe['currency_cost']:
                    raise ValueError("Insufficient currency")

            # Consume ingredients and currency
            await self._consume_ingredients(user_id, recipe['ingredients'])
            if recipe['currency_cost'] > 0:
                await self.currency.deduct_currency(
                    community_id,
                    user_id,
                    recipe['currency_cost'],
                    reason=f"Crafting: {recipe['result_item_name']}"
                )

            # Roll for success
            import random
            success = random.random() < recipe['success_rate']

            result = {
                'success': success,
                'recipe_name': recipe['name'],
                'currency_spent': recipe['currency_cost']
            }

            if success:
                # Grant crafted item
                item = await self.gear.grant_item(
                    community_id=community_id,
                    user_id=user_id,
                    item_id=recipe['result_item_id']
                )
                result['item'] = item

                # Check for bonus rare drop
                if recipe.get('rare_drop_chance') and random.random() < recipe['rare_drop_chance']:
                    rare_item = await self.gear.grant_item(
                        community_id=community_id,
                        user_id=user_id,
                        item_id=recipe['rare_drop_item_id']
                    )
                    result['bonus_item'] = rare_item

            # Set cooldown
            await self._set_cooldown(user_id, recipe_id, recipe['cooldown_seconds'])

            # Log crafting attempt
            await self.dal.execute("""
                INSERT INTO loyalty_crafting_log (
                    community_id, user_id, recipe_id, success,
                    currency_spent, created_at
                ) VALUES ($1, $2, $3, $4, $5, NOW())
            """, [community_id, user_id, recipe_id, success, recipe['currency_cost']])

            self.logger.audit(
                "Item crafted",
                community_id=community_id,
                user_id=user_id,
                recipe_id=recipe_id,
                success=success,
                result="SUCCESS"
            )

            return result

        except Exception as e:
            self.logger.error(f"Failed to craft item: {e}", community_id=community_id)
            raise
```

### Feature 4: Anti-Abuse Measures

**Abuse Patterns to Detect**:
- Bot farming (automated currency earning)
- Multi-accounting (same person, multiple accounts)
- Exploitation of bugs/glitches
- Market manipulation

```python
# loyalty_interaction_module/services/anti_abuse_service.py
from datetime import datetime, timedelta
from typing import List, Dict, Any

class AntiAbuseService:
    """
    Detect and prevent loyalty system abuse.

    Detection methods:
    - Rate limiting (max earnings per hour)
    - Behavioral analysis (unusual patterns)
    - IP address tracking (multi-accounting)
    - Transaction analysis (suspicious transfers)
    """

    def __init__(self, dal, logger):
        self.dal = dal
        self.logger = logger

    async def check_transaction(
        self,
        community_id: int,
        user_id: int,
        amount: int,
        transaction_type: str
    ) -> Dict[str, Any]:
        """
        Check if transaction is suspicious.

        Returns: {
            'allowed': bool,
            'reason': str,
            'confidence': float  # 0-1
        }
        """
        try:
            # Check rate limits
            if await self._exceeds_rate_limit(community_id, user_id, amount):
                return {
                    'allowed': False,
                    'reason': 'Rate limit exceeded',
                    'confidence': 1.0
                }

            # Check for bot patterns
            bot_score = await self._calculate_bot_score(community_id, user_id)
            if bot_score > 0.8:
                return {
                    'allowed': False,
                    'reason': 'Bot-like behavior detected',
                    'confidence': bot_score
                }

            # Check for multi-accounting
            if await self._detect_multi_account(community_id, user_id):
                return {
                    'allowed': False,
                    'reason': 'Multi-accounting suspected',
                    'confidence': 0.9
                }

            return {'allowed': True, 'reason': 'OK', 'confidence': 1.0}

        except Exception as e:
            self.logger.error(f"Failed to check transaction: {e}", community_id=community_id)
            # Fail open (allow transaction if check fails)
            return {'allowed': True, 'reason': 'Check failed', 'confidence': 0.0}

    async def _exceeds_rate_limit(
        self,
        community_id: int,
        user_id: int,
        amount: int
    ) -> bool:
        """Check if user exceeds hourly earning limit"""
        rows = await self.dal.execute("""
            SELECT SUM(amount) as total_earned
            FROM loyalty_transactions
            WHERE community_id = $1
            AND user_id = $2
            AND amount > 0
            AND created_at >= NOW() - INTERVAL '1 hour'
        """, [community_id, user_id])

        total_earned = rows[0]['total_earned'] or 0
        hourly_limit = 10000  # Configurable per community

        return (total_earned + amount) > hourly_limit

    async def _calculate_bot_score(self, community_id: int, user_id: int) -> float:
        """
        Calculate bot likelihood score (0-1).

        Factors:
        - Precise timing patterns (transactions every X seconds)
        - No variation in amounts
        - Active 24/7
        - Low chat activity
        """
        # Get transaction timing patterns
        rows = await self.dal.execute("""
            SELECT
                EXTRACT(EPOCH FROM (created_at - LAG(created_at) OVER (ORDER BY created_at))) as time_diff
            FROM loyalty_transactions
            WHERE community_id = $1
            AND user_id = $2
            ORDER BY created_at DESC
            LIMIT 100
        """, [community_id, user_id])

        if not rows or len(rows) < 10:
            return 0.0

        time_diffs = [row['time_diff'] for row in rows if row['time_diff']]

        # Calculate standard deviation of timing
        import statistics
        std_dev = statistics.stdev(time_diffs) if len(time_diffs) > 1 else 0

        # Low variance = bot-like (score inversely proportional to std dev)
        # If std_dev < 1 second, likely a bot
        if std_dev < 1.0:
            bot_score = 1.0 - (std_dev / 1.0)
        else:
            bot_score = 0.0

        return min(1.0, bot_score)
```

### Database Schema

```sql
-- Tournaments
CREATE TABLE loyalty_tournaments (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    tournament_type VARCHAR(50) NOT NULL,  -- bracket_single, leaderboard, etc.
    entry_fee INTEGER DEFAULT 0,
    prize_distribution FLOAT[] NOT NULL,    -- [0.5, 0.3, 0.2]
    max_participants INTEGER,
    start_time TIMESTAMP,
    status VARCHAR(50) DEFAULT 'registration',  -- registration, active, completed
    created_at TIMESTAMP DEFAULT NOW(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE loyalty_tournament_participants (
    id SERIAL PRIMARY KEY,
    tournament_id INTEGER REFERENCES loyalty_tournaments(id),
    user_id INTEGER NOT NULL,
    entry_fee_paid INTEGER DEFAULT 0,
    score INTEGER DEFAULT 0,
    rank INTEGER,
    prize_won INTEGER DEFAULT 0,
    registered_at TIMESTAMP DEFAULT NOW()
);

-- Crafting
CREATE TABLE loyalty_crafting_recipes (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    ingredients JSONB NOT NULL,            -- [{"item_id": 1, "quantity": 3}]
    currency_cost INTEGER DEFAULT 0,
    result_item_id INTEGER NOT NULL,
    success_rate FLOAT DEFAULT 1.0,        -- 0.0-1.0
    rare_drop_item_id INTEGER,
    rare_drop_chance FLOAT DEFAULT 0.0,
    cooldown_seconds INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE loyalty_crafting_log (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    recipe_id INTEGER REFERENCES loyalty_crafting_recipes(id),
    success BOOLEAN NOT NULL,
    currency_spent INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Anti-abuse
CREATE TABLE loyalty_abuse_flags (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    flag_type VARCHAR(50) NOT NULL,        -- rate_limit, bot_pattern, multi_account
    confidence FLOAT NOT NULL,             -- 0.0-1.0
    details JSONB,
    flagged_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,
    resolution VARCHAR(50)                 -- cleared, banned, warning
);
CREATE INDEX idx_abuse_flags ON loyalty_abuse_flags(community_id, user_id, flagged_at DESC);
```

---

## Implementation Timeline

### Phase 6.1: Core Modules (Weeks 1-3)
- **Week 1**: Identity OAuth verification, unified profile
- **Week 2**: Community health dashboard
- **Week 3**: Cross-platform broadcast system

### Phase 6.2: ML Bot Detection (Weeks 4-8)
- **Week 4-5**: Feature engineering and data preparation
- **Week 6**: Model training and validation
- **Week 7**: Deployment infrastructure
- **Week 8**: A/B testing and rollout

### Phase 6.3: GraphQL API (Weeks 6-9)
- **Week 6**: Schema design and types
- **Week 7**: Query and mutation resolvers
- **Week 8**: Subscriptions (real-time)
- **Week 9**: Testing and documentation

### Phase 6.4: Advanced Calendar (Weeks 7-10)
- **Week 7**: Platform sync (Discord, Twitch, Slack)
- **Week 8**: Recurring events (RRULE)
- **Week 9**: Reminder system
- **Week 10**: iCal export

### Phase 6.5: Enhanced Loyalty (Weeks 9-12)
- **Week 9**: Economy dashboard
- **Week 10**: Tournament system
- **Week 11**: Crafting system
- **Week 12**: Anti-abuse measures

**Note**: Weeks overlap as different sub-teams can work in parallel.

---

## Success Metrics

### Identity & Community
- **OAuth Verification Rate**: >80% of users verify via OAuth (vs whisper)
- **Cross-Platform Links**: Average 2.5+ platforms per user
- **Community Health Score**: >75 for active communities
- **Broadcast Reach**: 90%+ of community members receive announcements

### ML Bot Detection
- **Precision**: >85% (low false positives)
- **Recall**: >75% (catch most bots)
- **False Positive Rate**: <5%
- **Moderator Time Saved**: 30%+ reduction in bot review time

### GraphQL API
- **Adoption Rate**: 40%+ of API calls use GraphQL (vs REST) after 6 months
- **Query Performance**: p95 <200ms
- **Developer Satisfaction**: 4.5/5 stars
- **Subscription Usage**: 1000+ active WebSocket connections

### Advanced Calendar
- **Platform Sync Success**: >95% of events sync successfully
- **Reminder Delivery**: >98% of reminders sent on time
- **iCal Subscriptions**: 20%+ of communities subscribe to calendar feed
- **Event Attendance**: 10%+ increase with advanced features

### Enhanced Loyalty
- **Economy Health**: >80% of communities have health score >60
- **Tournament Participation**: 30%+ of active users join tournaments
- **Crafting Engagement**: 25%+ of users craft items monthly
- **Abuse Detection**: 90%+ of abuse attempts blocked

---

## Risk Assessment

### Technical Risks

**ML Model Accuracy** (High Impact, Medium Probability)
- **Risk**: Model has high false positive rate, angers users
- **Mitigation**: Extensive A/B testing, conservative thresholds, human review loop

**GraphQL Query Complexity** (Medium Impact, Medium Probability)
- **Risk**: Complex queries cause database overload
- **Mitigation**: Query depth/complexity limits, cost analysis, caching

**Platform API Rate Limits** (Medium Impact, High Probability)
- **Risk**: Discord/Twitch/Slack APIs rate limit Waddles
- **Mitigation**: Respect rate limits, implement backoff, queue syncs

**Recurring Event Bugs** (High Impact, Low Probability)
- **Risk**: RRULE parsing errors create duplicate/missing events
- **Mitigation**: Extensive testing, use battle-tested libraries (dateutil)

### Business Risks

**Feature Complexity** (Medium Impact, Medium Probability)
- **Risk**: Features too complex for average community manager
- **Mitigation**: Excellent documentation, guided setup, templates

**Maintenance Burden** (High Impact, Medium Probability)
- **Risk**: ML model requires continuous retraining and monitoring
- **Mitigation**: Automated pipelines, alerting, dedicated ML ops

**Third-Party Dependency** (Medium Impact, Low Probability)
- **Risk**: Platform APIs change, breaking integrations
- **Mitigation**: Version locking, feature flags, graceful degradation

---

## Conclusion

Phase 6 represents advanced features that differentiate Waddles in the market. While complex, these features provide significant value:

- **ML Bot Detection**: Automated moderation reduces workload
- **GraphQL API**: Modern, flexible API for integrations
- **Advanced Calendar**: Cross-platform event management
- **Enhanced Loyalty**: Deeper engagement and gamification

**Recommended Prioritization**:
1. **Start with**: Core module OAuth verification (removes whisper dependency)
2. **Then**: GraphQL API (enables better frontend experiences)
3. **Next**: ML Bot Detection (high value, complex)
4. **After**: Advanced Calendar (nice-to-have, good UX)
5. **Finally**: Enhanced Loyalty (engagement boost)

Each feature should be developed with:
- Comprehensive testing
- Feature flags for gradual rollout
- Monitoring and alerting
- Documentation and examples
- Backward compatibility

This phase transforms Waddles into a comprehensive community management platform ready for scale and differentiation in a competitive market.
